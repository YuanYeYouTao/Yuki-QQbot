"""Repositories for people, memberships, groups, and private access."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.rollup.db_models import (
    ConversationRollupJobModel,
    ConversationRollupModel,
    ConversationScopeModel,
)
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    ChatEventModel,
    GroupModel,
    MembershipModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryJobModel,
    PersonAliasModel,
    PersonModel,
    RuntimeConfigOverrideModel,
    WebSearchRunModel,
)
from qq_ai_bot.persistence.repository_helpers import (
    _ensure_group,
    _ensure_person,
    _ensure_relationship,
)
from qq_ai_bot.persistence.repository_records import (
    GroupSetting,
    PrivateUserSetting,
)


@dataclass(frozen=True, slots=True)
class GroupMemberNameMatch:
    user_id: str
    nickname: str
    group_card: str
    matched_alias: str
    score: float
    exact: bool

    @property
    def display_name(self) -> str:
        return self.group_card or self.nickname or self.user_id


def normalize_person_name(value: str) -> str:
    """Normalize identity labels without inferring any linguistic role."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character for character in normalized if unicodedata.category(character)[0] in {"L", "N"}
    )


class PeopleRepository:
    """Keep one global person and exact per-group memberships."""

    def __init__(
        self,
        database: Database,
        *,
        initial_affection: int = 50,
        initial_trust: int = 50,
        memory_rebuilds: MemoryRebuildRepository | None = None,
    ) -> None:
        self._database = database
        self._initial_affection = initial_affection
        self._initial_trust = initial_trust
        self._memory_rebuilds = memory_rebuilds

    async def affected_conversation_scopes(
        self,
        user_id: str,
    ) -> tuple[ConversationScope, ...]:
        """Resolve every conversation whose retained projection may mention a person."""

        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        ChatEventModel.bot_user_id,
                        ChatEventModel.scope_type,
                        ChatEventModel.group_id,
                        ChatEventModel.private_peer_user_id,
                    )
                    .where(
                        or_(
                            ChatEventModel.sender_user_id == user_id,
                            ChatEventModel.private_peer_user_id == user_id,
                            ChatEventModel.content.contains(user_id),
                            ChatEventModel.visual_summary.contains(user_id),
                            ChatEventModel.segments_json.contains(user_id),
                        )
                    )
                    .distinct()
                )
            ).all()
        scopes: dict[str, ConversationScope] = {}
        for bot_user_id, scope_type, group_id, private_peer_user_id in rows:
            if scope_type == ScopeType.GROUP.value and group_id is not None:
                scope = ConversationScope.group(str(bot_user_id), str(group_id))
            elif private_peer_user_id is not None:
                scope = ConversationScope.private(
                    str(bot_user_id),
                    str(private_peer_user_id),
                )
            else:
                continue
            scopes[scope.key] = scope
        return tuple(scopes[key] for key in sorted(scopes))

    async def observe(
        self,
        *,
        user_id: str,
        nickname: str,
        group_id: str | None = None,
        group_card: str = "",
        group_name: str = "",
        nickname_known: bool = True,
        group_card_known: bool = True,
        is_bot: bool = False,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
    ) -> None:
        """Update current values and retain historical aliases."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            person = await _ensure_person(
                session,
                user_id,
                nickname=nickname if nickname_known else "",
                is_bot=is_bot,
                now=now,
            )
            if not is_bot:
                await _ensure_relationship(
                    session,
                    user_id,
                    initial_affection=(
                        self._initial_affection if initial_affection is None else initial_affection
                    ),
                    initial_trust=(self._initial_trust if initial_trust is None else initial_trust),
                    now=now,
                )
            if nickname_known:
                person.nickname = nickname
            if nickname:
                await self._upsert_alias(session, user_id, "", nickname, "nickname", now)
            if group_id is None:
                return
            existing_group = await session.get(GroupModel, group_id)
            await _ensure_group(
                session,
                group_id,
                name=group_name,
                enabled=True if existing_group is None else None,
                now=now,
            )
            membership = await session.get(
                MembershipModel, {"user_id": user_id, "group_id": group_id}
            )
            if membership is None:
                membership = MembershipModel(
                    user_id=user_id,
                    group_id=group_id,
                    group_card=group_card if group_card_known else "",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(membership)
            else:
                if group_card_known:
                    membership.group_card = group_card
                membership.last_seen_at = now
            if group_card:
                await self._upsert_alias(session, user_id, group_id, group_card, "group_card", now)

    @staticmethod
    async def _upsert_alias(
        session: AsyncSession,
        user_id: str,
        group_scope: str,
        alias: str,
        alias_type: str,
        now: datetime,
    ) -> None:
        statement = insert(PersonAliasModel).values(
            user_id=user_id,
            group_scope=group_scope,
            alias=alias,
            alias_type=alias_type,
            first_seen_at=now,
            last_seen_at=now,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    PersonAliasModel.user_id,
                    PersonAliasModel.group_scope,
                    PersonAliasModel.alias,
                ],
                set_={"alias_type": alias_type, "last_seen_at": now},
            )
        )

    async def get(self, *, user_id: str, group_id: str | None = None) -> UserProfileSnapshot | None:
        async with self._database.sessions() as session:
            person = await session.get(PersonModel, user_id)
            if person is None:
                return None
            card = ""
            if group_id is not None:
                membership = await session.get(
                    MembershipModel, {"user_id": user_id, "group_id": group_id}
                )
                if membership is not None:
                    card = membership.group_card
            return UserProfileSnapshot(
                user_id=person.user_id,
                scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
                nickname=person.nickname,
                group_id=group_id,
                group_card=card,
            )

    async def get_many(
        self,
        user_ids: tuple[str, ...],
        *,
        group_id: str | None = None,
    ) -> dict[str, UserProfileSnapshot]:
        """Load several people and their current-group cards in one query."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return {}
        async with self._database.sessions() as session:
            statement = select(PersonModel, MembershipModel.group_card).outerjoin(
                MembershipModel,
                and_(
                    MembershipModel.user_id == PersonModel.user_id,
                    MembershipModel.group_id == group_id,
                ),
            )
            rows = (
                await session.execute(statement.where(PersonModel.user_id.in_(unique_ids)))
            ).all()
        return {
            person.user_id: UserProfileSnapshot(
                user_id=person.user_id,
                scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
                nickname=person.nickname,
                group_id=group_id,
                group_card=group_card or "",
            )
            for person, group_card in rows
        }

    async def aliases(self, user_id: str, *, limit: int = 20) -> tuple[str, ...]:
        async with self._database.sessions() as session:
            values = (
                await session.scalars(
                    select(PersonAliasModel.alias)
                    .where(PersonAliasModel.user_id == user_id)
                    .order_by(PersonAliasModel.last_seen_at.desc())
                    .limit(limit)
                )
            ).all()
            return tuple(dict.fromkeys(values))

    async def membership_count(self, user_id: str) -> int:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(MembershipModel)
                .where(MembershipModel.user_id == user_id)
            )
            return int(value or 0)

    async def members_in_group(
        self,
        user_ids: tuple[str, ...],
        group_id: str,
    ) -> frozenset[str]:
        """Return only identities with a real membership in the exact group."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return frozenset()
        async with self._database.sessions() as session:
            values = (
                await session.scalars(
                    select(MembershipModel.user_id).where(
                        MembershipModel.group_id == group_id,
                        MembershipModel.user_id.in_(unique_ids),
                    )
                )
            ).all()
        return frozenset(values)

    async def find_group_members_by_exact_name(
        self,
        name: str,
        group_id: str,
    ) -> tuple[str, ...]:
        """Resolve an exact nickname, group card, or in-scope alias inside one group."""

        normalized = name.strip()
        if not normalized:
            return ()
        alias_in_group = and_(
            PersonAliasModel.user_id == MembershipModel.user_id,
            PersonAliasModel.group_scope.in_(("", group_id)),
        )
        statement = (
            select(MembershipModel.user_id)
            .join(PersonModel, PersonModel.user_id == MembershipModel.user_id)
            .outerjoin(PersonAliasModel, alias_in_group)
            .where(
                MembershipModel.group_id == group_id,
                or_(
                    MembershipModel.group_card == normalized,
                    PersonModel.nickname == normalized,
                    PersonAliasModel.alias == normalized,
                ),
            )
            .distinct()
            .order_by(MembershipModel.user_id)
        )
        async with self._database.sessions() as session:
            values = (await session.scalars(statement)).all()
        return tuple(values)

    async def search_group_member_names(
        self,
        name: str,
        group_id: str,
        *,
        limit: int = 5,
        minimum_score: float = 0.35,
    ) -> tuple[GroupMemberNameMatch, ...]:
        """Return deterministic current-group identity candidates for a model-supplied name."""

        query = normalize_person_name(name)
        if not query or limit <= 0:
            return ()
        alias_in_group = and_(
            PersonAliasModel.user_id == MembershipModel.user_id,
            PersonAliasModel.group_scope.in_(("", group_id)),
        )
        statement = (
            select(
                MembershipModel.user_id,
                PersonModel.nickname,
                MembershipModel.group_card,
                PersonAliasModel.alias,
            )
            .join(PersonModel, PersonModel.user_id == MembershipModel.user_id)
            .outerjoin(PersonAliasModel, alias_in_group)
            .where(MembershipModel.group_id == group_id)
            .order_by(MembershipModel.user_id, PersonAliasModel.last_seen_at.desc())
        )
        async with self._database.sessions() as session:
            rows = (await session.execute(statement)).all()
        identities: dict[str, tuple[str, str, set[str]]] = {}
        for user_id, nickname, group_card, alias in rows:
            current = identities.setdefault(
                str(user_id),
                (str(nickname or ""), str(group_card or ""), set()),
            )
            if alias:
                current[2].add(str(alias))
        matches: list[GroupMemberNameMatch] = []
        for user_id, (nickname, group_card, aliases) in identities.items():
            labels = tuple(dict.fromkeys((group_card, nickname, *sorted(aliases))))
            best_alias = ""
            best_score = 0.0
            best_exact = False
            for label in labels:
                candidate = normalize_person_name(label)
                if not candidate:
                    continue
                exact = candidate == query
                if exact:
                    score = 1.0
                elif query in candidate or candidate in query:
                    score = 0.75 + 0.25 * (
                        min(len(query), len(candidate)) / max(len(query), len(candidate))
                    )
                else:
                    score = SequenceMatcher(None, query, candidate).ratio()
                if score > best_score or (
                    score == best_score and label.casefold() < best_alias.casefold()
                ):
                    best_alias = label
                    best_score = score
                    best_exact = exact
            if best_alias and best_score >= minimum_score:
                matches.append(
                    GroupMemberNameMatch(
                        user_id=user_id,
                        nickname=nickname,
                        group_card=group_card,
                        matched_alias=best_alias,
                        score=best_score,
                        exact=best_exact,
                    )
                )
        matches.sort(
            key=lambda item: (
                -item.score,
                item.display_name.casefold(),
                item.user_id,
            )
        )
        return tuple(matches[: min(limit, 5)])

    async def find_people_by_exact_name(self, name: str) -> tuple[str, ...]:
        """Resolve one exact nickname or historical alias across all conversations."""

        normalized = name.strip()
        if not normalized:
            return ()
        statement = (
            select(PersonModel.user_id)
            .outerjoin(PersonAliasModel, PersonAliasModel.user_id == PersonModel.user_id)
            .where(
                or_(
                    PersonModel.nickname == normalized,
                    PersonAliasModel.alias == normalized,
                )
            )
            .distinct()
            .order_by(PersonModel.user_id)
        )
        async with self._database.sessions() as session:
            values = (await session.scalars(statement)).all()
        return tuple(values)

    async def set_enabled(
        self,
        user_id: str,
        enabled: bool,
        *,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.set_enabled(
                    user_id,
                    enabled,
                    initial_affection=initial_affection,
                    initial_trust=initial_trust,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        person = await _ensure_person(session, user_id, now=now)
        await _ensure_relationship(
            session,
            user_id,
            initial_affection=(
                self._initial_affection if initial_affection is None else initial_affection
            ),
            initial_trust=(self._initial_trust if initial_trust is None else initial_trust),
            now=now,
        )
        person.enabled = enabled
        await session.flush()
        return PrivateUserSetting(user_id=user_id, enabled=enabled)

    async def get_enabled(
        self,
        user_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting | None:
        if session is None:
            async with self._database.sessions() as owned_session:
                return await self.get_enabled(user_id, session=owned_session)
        person = await session.get(PersonModel, user_id)
        if person is None:
            return None
        return PrivateUserSetting(user_id=user_id, enabled=person.enabled)

    async def delete_person(self, user_id: str, *, marker: str = "[已删除用户]") -> bool:
        """Delete all attributable data and redact exact QQ text elsewhere."""

        async with self._database.immediate_session() as session:
            person = await session.get(PersonModel, user_id)
            if person is None:
                return False
            affected_scopes = await self.affected_conversation_scopes_in_session(
                session,
                user_id,
            )
            privacy_event_match = or_(
                ChatEventModel.sender_user_id == user_id,
                ChatEventModel.private_peer_user_id == user_id,
                ChatEventModel.content.contains(user_id),
                ChatEventModel.visual_summary.contains(user_id),
                ChatEventModel.segments_json.contains(user_id),
            )
            private_scope_keys = tuple(
                scope.key
                for scope in affected_scopes
                if scope.scope_type is ScopeType.PRIVATE and scope.private_peer_user_id == user_id
            )
            web_cleanup_conditions = [
                WebSearchRunModel.trigger_message_id.in_(
                    select(ChatEventModel.platform_message_id).where(privacy_event_match)
                ),
                WebSearchRunModel.conversation_key == f"private:{user_id}",
                WebSearchRunModel.conversation_key.like(f"group:%:user:{user_id}"),
            ]
            if private_scope_keys:
                web_cleanup_conditions.append(
                    WebSearchRunModel.conversation_key.in_(private_scope_keys)
                )
            await session.execute(delete(WebSearchRunModel).where(or_(*web_cleanup_conditions)))
            now = datetime.now(UTC)
            for scope in affected_scopes:
                scope_row = await session.scalar(
                    select(ConversationScopeModel).where(
                        ConversationScopeModel.scope_key == scope.key
                    )
                )
                if scope_row is None:
                    continue
                await session.execute(
                    delete(ConversationRollupModel).where(
                        ConversationRollupModel.scope_id == scope_row.id
                    )
                )
                await session.execute(
                    delete(ConversationRollupJobModel).where(
                        ConversationRollupJobModel.scope_id == scope_row.id
                    )
                )
                if scope.scope_type is ScopeType.PRIVATE and scope.private_peer_user_id == user_id:
                    await session.delete(scope_row)
                else:
                    scope_row.generation += 1
                    scope_row.starts_after_event_id = scope_row.last_event_id
                    scope_row.last_generation_change_event_id = 0
                    scope_row.uncovered_event_count = 0
                    scope_row.uncovered_character_count = 0
                    scope_row.updated_at = now
            if self._memory_rebuilds is not None:
                await self._memory_rebuilds.forget_person(user_id, session=session)
            attributable_event_ids = (
                await session.scalars(
                    select(ChatEventModel.id).where(
                        or_(
                            ChatEventModel.sender_user_id == user_id,
                            ChatEventModel.private_peer_user_id == user_id,
                        )
                    )
                )
            ).all()
            remaining = (
                await session.scalars(
                    select(ChatEventModel).where(
                        ChatEventModel.sender_user_id != user_id,
                        or_(
                            ChatEventModel.private_peer_user_id.is_(None),
                            ChatEventModel.private_peer_user_id != user_id,
                        ),
                        or_(
                            ChatEventModel.content.contains(user_id),
                            ChatEventModel.visual_summary.contains(user_id),
                            ChatEventModel.segments_json.contains(user_id),
                        ),
                    )
                )
            ).all()
            for event in remaining:
                event.content = event.content.replace(user_id, marker)
                event.visual_summary = event.visual_summary.replace(user_id, marker)
                event.segments_json = event.segments_json.replace(user_id, marker)
                now = datetime.now(UTC)
                job_statement = insert(MemoryJobModel).values(
                    event_id=event.id,
                    conversation_key=(
                        f"group:{event.group_id}:user:{event.sender_user_id}"
                        if event.group_id
                        else f"private:{event.private_peer_user_id or event.sender_user_id}"
                    ),
                    status="pending",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                    error_category=None,
                )
                await session.execute(
                    job_statement.on_conflict_do_update(
                        index_elements=[MemoryJobModel.event_id],
                        set_={
                            "status": "pending",
                            "attempts": 0,
                            "conversation_key": (
                                f"group:{event.group_id}:user:{event.sender_user_id}"
                                if event.group_id
                                else f"private:{event.private_peer_user_id or event.sender_user_id}"
                            ),
                            "next_attempt_at": now,
                            "updated_at": now,
                            "error_category": None,
                        },
                    )
                )
            affected_fact_ids = select(MemoryEvidenceModel.fact_id).where(
                MemoryEvidenceModel.event_id.in_(attributable_event_ids)
            )
            await session.execute(
                delete(MemoryFactModel).where(
                    MemoryFactModel.source_type != "explicit",
                    or_(
                        MemoryFactModel.content.contains(user_id),
                        MemoryFactModel.id.in_(affected_fact_ids),
                    ),
                )
            )
            await session.execute(
                delete(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.scope_type == "user",
                    RuntimeConfigOverrideModel.scope_id == user_id,
                )
            )
            await session.execute(
                update(RuntimeConfigOverrideModel)
                .where(RuntimeConfigOverrideModel.updated_by == user_id)
                .values(updated_by=marker)
            )
            audit_rows = (
                await session.scalars(
                    select(AdminOperationEventModel).where(
                        or_(
                            AdminOperationEventModel.actor_user_id == user_id,
                            AdminOperationEventModel.target_id == user_id,
                            AdminOperationEventModel.conversation_key.contains(user_id),
                            AdminOperationEventModel.before_json.contains(user_id),
                            AdminOperationEventModel.after_json.contains(user_id),
                        )
                    )
                )
            ).all()
            for audit in audit_rows:
                if audit.actor_user_id == user_id:
                    audit.actor_user_id = marker
                if audit.target_id == user_id:
                    audit.target_id = marker
                audit.conversation_key = audit.conversation_key.replace(user_id, marker)
                audit.before_json = audit.before_json.replace(user_id, marker)
                audit.after_json = audit.after_json.replace(user_id, marker)
            await session.delete(person)
            return True

    @staticmethod
    async def affected_conversation_scopes_in_session(
        session: AsyncSession,
        user_id: str,
    ) -> tuple[ConversationScope, ...]:
        rows = (
            await session.execute(
                select(
                    ChatEventModel.bot_user_id,
                    ChatEventModel.scope_type,
                    ChatEventModel.group_id,
                    ChatEventModel.private_peer_user_id,
                )
                .where(
                    or_(
                        ChatEventModel.sender_user_id == user_id,
                        ChatEventModel.private_peer_user_id == user_id,
                        ChatEventModel.content.contains(user_id),
                        ChatEventModel.visual_summary.contains(user_id),
                        ChatEventModel.segments_json.contains(user_id),
                    )
                )
                .distinct()
            )
        ).all()
        scopes: dict[str, ConversationScope] = {}
        for bot_user_id, scope_type, group_id, private_peer_user_id in rows:
            if scope_type == ScopeType.GROUP.value and group_id is not None:
                scope = ConversationScope.group(str(bot_user_id), str(group_id))
            elif private_peer_user_id is not None:
                scope = ConversationScope.private(
                    str(bot_user_id),
                    str(private_peer_user_id),
                )
            else:
                continue
            scopes[scope.key] = scope
        return tuple(scopes[key] for key in sorted(scopes))


class UserProfileRepository(PeopleRepository):
    """Backward-compatible name used by identity services."""

    async def upsert(
        self,
        *,
        user_id: str,
        nickname: str,
        group_id: str | None = None,
        group_card: str = "",
        nickname_known: bool = True,
        group_card_known: bool = True,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
    ) -> None:
        await self.observe(
            user_id=user_id,
            nickname=nickname,
            group_id=group_id,
            group_card=group_card,
            nickname_known=nickname_known,
            group_card_known=group_card_known,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
        )

    async def delete_user(self, user_id: str) -> bool:
        return await self.delete_person(user_id)


class GroupSettingsRepository:
    """Persist group observation and autonomous participation settings."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(
        self,
        group_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> GroupSetting | None:
        if session is None:
            async with self._database.sessions() as owned_session:
                return await self.get(group_id, session=owned_session)
        row = await session.get(GroupModel, group_id)
        if row is None:
            return None
        return GroupSetting(
            group_id=group_id,
            enabled=row.enabled,
            require_mention=row.require_mention,
            autonomous_enabled=row.autonomous_enabled,
            name=row.name,
        )

    async def set_enabled(
        self,
        group_id: str,
        enabled: bool,
        *,
        session: AsyncSession | None = None,
    ) -> GroupSetting:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.set_enabled(group_id, enabled, session=owned_session)
        now = datetime.now(UTC)
        row = await _ensure_group(session, group_id, enabled=enabled, now=now)
        await session.flush()
        return GroupSetting(
            group_id=group_id,
            enabled=enabled,
            require_mention=row.require_mention,
            autonomous_enabled=row.autonomous_enabled,
            name=row.name,
        )

    async def set_autonomous_enabled(
        self,
        group_id: str,
        enabled: bool,
        *,
        session: AsyncSession | None = None,
    ) -> GroupSetting:
        """Update only the group's autonomous participation switch."""

        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.set_autonomous_enabled(
                    group_id,
                    enabled,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        row = await _ensure_group(session, group_id, now=now)
        row.autonomous_enabled = enabled
        row.updated_at = now
        await session.flush()
        return GroupSetting(
            group_id=group_id,
            enabled=row.enabled,
            require_mention=row.require_mention,
            autonomous_enabled=enabled,
            name=row.name,
        )

    async def observe(
        self,
        group_id: str,
        *,
        name: str = "",
        enabled_if_new: bool = False,
    ) -> GroupSetting:
        """Create an observed group without overwriting an existing access switch."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            existing = await session.get(GroupModel, group_id)
            row = await _ensure_group(
                session,
                group_id,
                name=name,
                enabled=enabled_if_new if existing is None else None,
                now=now,
            )
        return GroupSetting(
            group_id=group_id,
            enabled=row.enabled,
            require_mention=row.require_mention,
            autonomous_enabled=row.autonomous_enabled,
            name=row.name,
        )


class PrivateUserSettingsRepository:
    """Private chats are allowed unless the person's row explicitly disables them."""

    def __init__(
        self,
        database: Database,
        *,
        initial_affection: int = 50,
        initial_trust: int = 50,
    ) -> None:
        self._people = PeopleRepository(
            database,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
        )

    async def get(
        self,
        user_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting | None:
        return await self._people.get_enabled(user_id, session=session)

    async def set_enabled(
        self,
        user_id: str,
        enabled: bool,
        *,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting:
        return await self._people.set_enabled(
            user_id,
            enabled,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
            session=session,
        )
