import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Any

import aiohttp
import asyncpg
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("report")

API_VERSION = "5.199"

TOKEN = os.getenv("GROUP_TOKEN", "").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "0") or 0)
DEV_ID = int(os.getenv("DEV_ID", "0") or 0)
CONFIRMATION_CODE = os.getenv("CONFIRMATION_CODE", "").strip()
CHAT_REPLY = int(os.getenv("CHAT_REPLY", "0") or 0)
READ_CHATS = frozenset(
    int(chunk)
    for chunk in re.split(r"[,\s;]+", os.getenv("CHAT_READ", ""))
    if chunk.isdigit()
)
DATABASE_URL = os.getenv("DATABASE", "").strip()
PORT = int(os.getenv("PORT", "8080") or 8080)
PING_URL = (os.getenv("PUBLIC_URL", "") or os.getenv("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "600") or 600)

REPORT_COMMANDS = frozenset({"/rep", "/жб", "жб", "rep"})
MUTE_COMMANDS = frozenset({"/muterep", "muterep"})
UNMUTE_COMMANDS = frozenset({"/unmuterep", "unmuterep"})
TAKE_COMMANDS = frozenset({"ответить", "взять"})
CANCEL_COMMANDS = frozenset({"отмена", "отменить"})

DEDUPE_SECONDS = 5
WATCH_INTERVAL = 10

MENTION_RE = re.compile(r"\[(?:id|club|public)(-?\d+)\|")
AT_ID_RE = re.compile(r"@\s*(?:id)?(\d{2,})(?![\w\d])")
STRIP_MENTION_RE = re.compile(
    r"@?\s*\[(?:id|club|public)-?\d+\|[^\]]*\]\s*\|?"
    r"|@\s*(?:id)?\d{2,}"
    r"|(?<![\w@])@(?!\w)"
)
DURATION_RE = re.compile(r"(\d{1,4})\s*([а-яёa-z]+)")

DURATION_UNITS: dict[str, int] = {}
for words, factor in (
    (("секунда", "секунды", "секунд", "сек", "с"), 1),
    (("минута", "минуты", "минут", "мин", "м"), 60),
    (("час", "часа", "часов", "ч"), 3600),
    (("день", "дня", "дней", "сутки", "суток", "д"), 86400),
    (("неделя", "недели", "недель", "нед", "н"), 604800),
    (("месяц", "месяца", "месяцев", "мес"), 2592000),
):
    for word in words:
        DURATION_UNITS[word] = factor

ATTACHMENT_NAMES = {
    "photo": "фото",
    "video": "видео",
    "audio": "аудио",
    "doc": "документ",
    "link": "ссылка",
    "wall": "запись со стены",
    "sticker": "стикер",
    "market": "товар",
    "audio_message": "голосовое",
    "video_message": "видеосообщение",
}

DEFAULT_MUTE_REASON = "нарушение правил чата"


def to_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return 0


def now_ts() -> int:
    return int(time.time())


def clean(text: str) -> str:
    return " ".join((text or "").split())


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def split_command(text: str) -> tuple[str, str]:
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""


def plural(number: int, one: str, few: str, many: str) -> str:
    number = abs(number) % 100
    if 11 <= number <= 14:
        return many
    number %= 10
    if number == 1:
        return one
    if 2 <= number <= 4:
        return few
    return many


def humanize(seconds: int) -> str:
    seconds = max(int(seconds), 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} {plural(days, 'день', 'дня', 'дней')}")
    if hours:
        parts.append(f"{hours} {plural(hours, 'час', 'часа', 'часов')}")
    if minutes:
        parts.append(f"{minutes} {plural(minutes, 'минуту', 'минуты', 'минут')}")
    if secs or not parts:
        parts.append(f"{secs} {plural(secs, 'секунду', 'секунды', 'секунд')}")
    return " ".join(parts)


def strip_mentions(text: str) -> str:
    return clean(STRIP_MENTION_RE.sub(" ", text or ""))


def extract_target(text: str) -> tuple[int, str]:
    text = text or ""
    match = MENTION_RE.search(text) or AT_ID_RE.search(text)
    if match:
        return to_int(match.group(1)), strip_mentions(text)
    head = text.strip().split(maxsplit=1)
    if head and head[0].isdigit() and len(head[0]) >= 4:
        return int(head[0]), " ".join(head[1:])
    return 0, text


def extract_duration(text: str) -> tuple[int, str]:
    match = DURATION_RE.search(text or "")
    if not match:
        return 0, text or ""
    factor = DURATION_UNITS.get(match.group(2).lower().replace("ё", "е").strip(".,!?-"))
    if not factor:
        return 0, text or ""
    seconds = int(match.group(1)) * factor
    rest = text[: match.start()] + " " + text[match.end():]
    return seconds, rest


def extract_reply(message: dict | None) -> dict:
    message = message or {}
    reply = message.get("reply_message")
    if isinstance(reply, dict) and reply:
        return reply
    forwards = message.get("fwd_messages") or []
    if isinstance(forwards, list):
        for item in forwards:
            if isinstance(item, dict) and item:
                return item
    return {}


def describe_attachments(message: dict | None) -> str:
    counter: dict[str, int] = {}
    links: list[str] = []
    for item in (message or {}).get("attachments") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type") or "other"
        counter[kind] = counter.get(kind, 0) + 1
        body = item.get(kind)
        if isinstance(body, dict):
            link = body.get("link") or body.get("url") or body.get("title")
            if isinstance(link, str) and link and link not in links:
                links.append(link)
    lines = [
        f"📎 {ATTACHMENT_NAMES.get(kind, kind)} × {count}" for kind, count in counter.items()
    ]
    lines.extend(f"🔗 {truncate(link, 150)}" for link in links[:3])
    return "\n".join(lines)


def callback_button(label: str, payload: str, color: str = "primary") -> dict:
    return {
        "action": {
            "type": "callback",
            "label": label,
            "payload": json.dumps(payload),
        }
    }


def text_button(command: str) -> dict:
    return {"action": {"type": "text", "label": command}}


def build_keyboard(*buttons: dict) -> str:
    return json.dumps(
        {"one_time": False, "inline": True, "buttons": [[button] for button in buttons]},
        ensure_ascii=False,
    )


def is_event(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    event_type = payload.get("type")
    return isinstance(event_type, str) and (
        "message" in event_type
        or event_type in ("button_event", "message_event", "board_post_new")
    )


class VKError(Exception):
    def __init__(self, method: str, error: dict | None) -> None:
        self.method = method
        self.error = error or {}
        super().__init__(f"{method}: {self.error.get('error_msg', 'unknown error')}")


class VK:
    def __init__(self, token: str, group_id: int) -> None:
        self.token = token
        self.group_id = group_id
        self.session: aiohttp.ClientSession | None = None
        self._names: dict[int, str] = {}
        self._no_reply: set[int] = set()

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            connector=aiohttp.TCPConnector(limit=20, ssl=False),
        )

    async def stop(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def call(self, method: str, **params: Any) -> Any:
        if self.session is None:
            raise RuntimeError("VK client is not started")
        payload = dict(params)
        payload["access_token"] = self.token
        payload["v"] = API_VERSION
        failure: VKError | None = None
        for attempt in range(3):
            async with self.session.post(
                f"https://api.vk.com/method/{method}", data=payload
            ) as response:
                data = await response.json()
            if not isinstance(data, dict) or "error" not in data:
                return data.get("response") if isinstance(data, dict) else None
            failure = VKError(method, data["error"])
            if data["error"].get("error_code") in (6, 9, 10) and attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            break
        raise failure or VKError(method, None)

    async def send(
        self,
        peer_id: int,
        message: str,
        reply_cmid: int = 0,
        keyboard: str | None = None,
        reply_mid: int = 0,
    ) -> int:
        params: dict[str, Any] = {
            "peer_ids": str(peer_id),
            "message": message,
            "random_id": random.randint(1, 2**31 - 1),
        }
        if keyboard:
            params["keyboard"] = keyboard
        variants: list[dict[str, Any]] = []
        if reply_cmid:
            variants.append(
                {
                    "forward": json.dumps(
                        {
                            "peer_id": peer_id,
                            "conversation_message_ids": [int(reply_cmid)],
                            "is_reply": 1,
                        }
                    )
                }
            )
        if reply_mid:
            variants.append({"reply_to": int(reply_mid)})
        result = None
        replied = False
        for extra in variants + [{}]:
            try:
                result = await self.call("messages.send", **params, **extra)
                replied = bool(extra)
                break
            except VKError as exc:
                if not extra:
                    raise
                logger.debug("Реплай не удался (%s), пробую дальше", exc)
        if not replied and variants and peer_id not in self._no_reply:
            self._no_reply.add(peer_id)
            logger.warning("В чате %s не удалось ответить реплаем", peer_id)
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return to_int(result[0].get("conversation_message_id"))
        return 0

    async def delete(self, peer_id: int, conversation_message_id: int) -> None:
        if not conversation_message_id:
            return
        try:
            await self.call(
                "messages.delete",
                peer_id=peer_id,
                cmids=conversation_message_id,
                delete_for_all=1,
            )
        except VKError as exc:
            logger.warning("Не удалось удалить сообщение %s: %s", conversation_message_id, exc)

    async def user_name(self, user_id: int) -> str:
        if user_id in self._names:
            return self._names[user_id]
        name = ""
        try:
            result = await self.call("users.get", user_ids=user_id)
            if result:
                user = result[0]
                name = clean(f"{user.get('first_name', '')} {user.get('last_name', '')}")
                if not name:
                    name = clean(user.get("name", ""))
        except VKError as exc:
            logger.warning("Не удалось получить имя %s: %s", user_id, exc)
        if not name:
            name = f"id{user_id}"
        self._names[user_id] = name
        return name

    async def mention(self, user_id: int) -> str:
        if not user_id:
            return "не указан"
        return f"[id{user_id}|{await self.user_name(user_id)}]"

    async def message(self, peer_id: int, conversation_message_id: int) -> dict:
        try:
            result = await self.call(
                "messages.getByConversationMessageId",
                peer_id=peer_id,
                conversation_message_ids=conversation_message_id,
            )
        except VKError as exc:
            logger.warning("Не удалось получить сообщение %s: %s", conversation_message_id, exc)
            return {}
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        return {}

    async def delete_board_comment(self, topic_id: int, comment_id: int) -> bool:
        if not topic_id or not comment_id:
            return False
        try:
            await self.call(
                "board.deleteComment",
                group_id=self.group_id,
                topic_id=topic_id,
                comment_id=comment_id,
            )
            return True
        except VKError as exc:
            logger.warning(
                "Не удалось удалить комментарий %s в обсуждении %s: %s",
                comment_id,
                topic_id,
                exc,
            )
            return False


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=3)
        self.pool = pool
        try:
            await self.migrate()
        except Exception:
            self.pool = None
            await pool.close()
            raise

    async def migrate(self) -> None:
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS rep_mutes (
                user_id BIGINT PRIMARY KEY,
                admin_id BIGINT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                until_ts BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS rep_reports (
                id BIGSERIAL PRIMARY KEY,
                author_id BIGINT NOT NULL,
                violator_id BIGINT,
                reason TEXT NOT NULL DEFAULT '',
                reply_text TEXT NOT NULL DEFAULT '',
                attachments TEXT NOT NULL DEFAULT '',
                peer_id BIGINT NOT NULL DEFAULT 0,
                cmid BIGINT NOT NULL DEFAULT 0,
                msg_id BIGINT NOT NULL DEFAULT 0,
                message_cmid BIGINT,
                prompt_cmid BIGINT,
                admin_id BIGINT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await self.pool.execute(
            "ALTER TABLE rep_reports ADD COLUMN IF NOT EXISTS msg_id BIGINT NOT NULL DEFAULT 0"
        )
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS rep_reports_cmid ON rep_reports (cmid)"
        )
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS rep_reports_prompt ON rep_reports (prompt_cmid)"
        )
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS rep_reports_message ON rep_reports (message_cmid)"
        )
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS rep_board_seen (
                topic_id BIGINT NOT NULL,
                comment_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (topic_id, comment_id)
            )
            """
        )
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS rep_board_seen_user ON rep_board_seen (user_id)"
        )

    async def save_board_seen(self, topic_id: int, comment_id: int, user_id: int) -> None:
        if not topic_id or not comment_id or user_id <= 0:
            return
        try:
            await self.pool.execute(
                """
                INSERT INTO rep_board_seen (topic_id, comment_id, user_id) VALUES ($1, $2, $3)
                ON CONFLICT (topic_id, comment_id) DO NOTHING
                """,
                topic_id,
                comment_id,
                user_id,
            )
        except Exception as exc:
            logger.warning("Не удалось сохранить комментарий %s: %s", comment_id, exc)

    async def board_by_user(self, user_id: int) -> list[asyncpg.Record]:
        return list(
            await self.pool.fetch(
                "SELECT topic_id, comment_id FROM rep_board_seen WHERE user_id = $1 ORDER BY created_at",
                user_id,
            )
        )

    async def forget_board(self, user_id: int, topic_id: int, comment_ids: list[int]) -> None:
        if not comment_ids:
            return
        try:
            await self.pool.execute(
                "DELETE FROM rep_board_seen WHERE user_id = $1 AND topic_id = $2 AND comment_id = ANY($3::bigint[])",
                user_id,
                topic_id,
                comment_ids,
            )
        except Exception as exc:
            logger.warning("Не удалось очистить журнал комментариев: %s", exc)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def get_mute(self, user_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT user_id, admin_id, reason, until_ts FROM rep_mutes WHERE user_id = $1",
            user_id,
        )

    async def set_mute(self, user_id: int, admin_id: int, reason: str, until_ts: int) -> None:
        await self.pool.execute(
            """
            INSERT INTO rep_mutes (user_id, admin_id, reason, until_ts)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE
            SET admin_id = EXCLUDED.admin_id,
                reason = EXCLUDED.reason,
                until_ts = EXCLUDED.until_ts
            """,
            user_id,
            admin_id,
            reason,
            until_ts,
        )

    async def delete_mute(self, user_id: int) -> None:
        await self.pool.execute("DELETE FROM rep_mutes WHERE user_id = $1", user_id)

    async def expired_mutes(self) -> list[asyncpg.Record]:
        return list(
            await self.pool.fetch(
                "SELECT user_id, reason, until_ts FROM rep_mutes WHERE until_ts <= $1",
                now_ts(),
            )
        )

    async def create_report(
        self,
        author_id: int,
        violator_id: int,
        reason: str,
        reply_text: str,
        attachments: str,
        peer_id: int,
        cmid: int,
        msg_id: int = 0,
    ) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO rep_reports
                (author_id, violator_id, reason, reply_text, attachments, peer_id, cmid, msg_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            """,
            author_id,
            violator_id or None,
            reason,
            reply_text,
            attachments,
            peer_id,
            cmid,
            msg_id or 0,
        )
        return to_int(row["id"])

    async def get_report(self, report_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM rep_reports WHERE id = $1", report_id
        )

    async def set_message_cmid(self, report_id: int, message_cmid: int) -> None:
        await self.pool.execute(
            "UPDATE rep_reports SET message_cmid = $2 WHERE id = $1",
            report_id,
            message_cmid,
        )

    async def find_report_by_cmid(self, conversation_message_id: int) -> asyncpg.Record | None:
        if not conversation_message_id:
            return None
        return await self.pool.fetchrow(
            """
            SELECT * FROM rep_reports
            WHERE prompt_cmid = $1 OR message_cmid = $1
            ORDER BY id DESC LIMIT 1
            """,
            conversation_message_id,
        )

    async def take_report(self, report_id: int, prompt_cmid: int, admin_id: int) -> None:
        await self.pool.execute(
            """
            UPDATE rep_reports
            SET prompt_cmid = $2, admin_id = $3, status = 'taken'
            WHERE id = $1
            """,
            report_id,
            prompt_cmid,
            admin_id,
        )

    async def cancel_report(self, report_id: int, message_cmid: int) -> None:
        await self.pool.execute(
            """
            UPDATE rep_reports
            SET message_cmid = $2, prompt_cmid = NULL, admin_id = NULL, status = 'new'
            WHERE id = $1
            """,
            report_id,
            message_cmid,
        )

    async def close_report(self, report_id: int, admin_id: int) -> None:
        await self.pool.execute(
            "UPDATE rep_reports SET admin_id = $2, status = 'done' WHERE id = $1",
            report_id,
            admin_id,
        )


class ReportBot:
    def __init__(self, vk: VK, db: Database) -> None:
        self.vk = vk
        self.db = db
        self._recent: dict[tuple, float] = {}

    def is_admin(self, user_id: int) -> bool:
        return bool(DEV_ID) and user_id == DEV_ID

    def is_duplicate(self, key: tuple) -> bool:
        moment = time.monotonic()
        self._recent = {k: v for k, v in self._recent.items() if moment - v < DEDUPE_SECONDS}
        if key in self._recent:
            return True
        self._recent[key] = moment
        return False

    async def handle_event(self, event: dict) -> None:
        event_type = event.get("type") or ""
        obj = event.get("object") or {}
        if event_type in ("button_event", "message_event"):
            logger.info("событие %s", event_type)
            await self.on_button(obj)
        elif event_type == "board_post_new":
            await self.on_board_comment(obj)
        elif "message" in event_type:
            await self.on_message(obj)

    async def on_message(self, obj: dict) -> None:
        message = obj.get("message") or {}
        peer_id = to_int(message.get("peer_id"))
        from_id = to_int(message.get("from_id"))
        text = (message.get("text") or "").strip()
        if not peer_id or from_id <= 0 or not text:
            return
        cmid = to_int(obj.get("conversation_message_id")) or to_int(
            message.get("conversation_message_id")
        )
        mid = to_int(message.get("id"))
        command, rest = split_command(text)
        logger.info(
            "chat %s | user %s | cmid %s | id %s | %s", peer_id, from_id, cmid, mid, text
        )
        if command in REPORT_COMMANDS and (peer_id in READ_CHATS or peer_id == from_id):
            await self.on_user_message(peer_id, from_id, text, cmid, message, mid)
            return
        if peer_id != CHAT_REPLY:
            return
        if command in MUTE_COMMANDS:
            await self.mute_user(peer_id, from_id, cmid, rest)
        elif command in UNMUTE_COMMANDS:
            await self.unmute_user(peer_id, from_id, cmid, rest)
        elif command in TAKE_COMMANDS and rest.strip().isdigit():
            await self.take_command(peer_id, from_id, to_int(rest), cmid)
        elif command in CANCEL_COMMANDS and rest.strip().isdigit():
            await self.cancel_command(peer_id, from_id, to_int(rest), cmid)
        else:
            await self.answer_flow(peer_id, from_id, text, cmid, message, mid)

    async def on_user_message(
        self,
        peer_id: int,
        from_id: int,
        text: str,
        cmid: int,
        message: dict,
        mid: int = 0,
    ) -> None:
        command, rest = split_command(text)
        if command not in REPORT_COMMANDS:
            return
        mute = await self.db.get_mute(from_id)
        if mute is not None:
            left = humanize(to_int(mute["until_ts"]) - now_ts())
            await self.vk.send(
                peer_id,
                f"🚫 {await self.vk.mention(from_id)}, у вас заблокирован доступ к репорту."
                f"\nОсталось до снятия: {left}",
                reply_cmid=cmid,
                reply_mid=mid,
            )
            return
        reply = extract_reply(message)
        reply_text = truncate(clean(reply.get("text")), 400)
        attachments = describe_attachments(reply)
        violator_id = to_int(reply.get("from_id"))
        if violator_id <= 0:
            violator_id, rest = extract_target(rest)
        else:
            rest = strip_mentions(rest)
        reason = clean(rest)
        if not reply_text and not reason and not violator_id:
            return
        if self.is_duplicate((from_id, violator_id, reply_text[:60], reason[:60])):
            return
        report_id = await self.db.create_report(
            from_id,
            violator_id,
            reason,
            reply_text,
            attachments,
            peer_id,
            cmid,
            mid,
        )
        body = await self.report_body(
            report_id, from_id, violator_id, reason, reply_text, attachments
        )
        report_cmid = await self.vk.send(
            CHAT_REPLY,
            body,
            keyboard=build_keyboard(callback_button("Ответить", f"rep:{report_id}")),
        )
        await self.db.set_message_cmid(report_id, report_cmid)
        await self.vk.send(
            peer_id,
            "✅ Жалоба была отправлена администрации чата",
            reply_cmid=cmid,
            reply_mid=mid,
        )

    async def report_body(
        self,
        report_id: int,
        author_id: int,
        violator_id: int,
        reason: str,
        reply_text: str,
        attachments: str,
    ) -> str:
        lines = [
            f"🆘 Новый репорт от: {await self.vk.mention(author_id)}",
            f"Нарушитель: {await self.vk.mention(violator_id) if violator_id else 'не указан'}",
        ]
        if reply_text:
            lines.append(f"Реплай: {reply_text}")
        if attachments:
            lines.append(attachments)
        if reason:
            lines.append(f"Текст: {reason}")
        lines.append(f"Репорт №{report_id}")
        return "\n".join(lines)

    async def answer_flow(
        self, peer_id: int, from_id: int, text: str, cmid: int, message: dict, mid: int = 0
    ) -> None:
        reply = extract_reply(message)
        if not reply and cmid:
            reply = extract_reply(await self.vk.message(peer_id, cmid))
        reply_cmid = to_int(reply.get("conversation_message_id"))
        if not reply_cmid:
            return
        report = await self.db.find_report_by_cmid(reply_cmid)
        if report is None or report["status"] != "taken":
            return
        if to_int(report["prompt_cmid"]) != reply_cmid:
            return
        answer = truncate(text, 900)
        user_chat = to_int(report["peer_id"]) or next(iter(READ_CHATS), 0)
        await self.vk.send(
            user_chat,
            f"💬 Администратор ответил на вашу жалобу: {answer}",
            reply_cmid=to_int(report["cmid"]),
            reply_mid=to_int(report["msg_id"]),
        )
        if report["prompt_cmid"]:
            await self.vk.delete(peer_id, to_int(report["prompt_cmid"]))
        await self.vk.send(
            peer_id, "✅ Ваш ответ был отправлен пользователю.", reply_cmid=cmid, reply_mid=mid
        )
        await self.db.close_report(to_int(report["id"]), from_id)

    async def on_button(self, obj: dict) -> None:
        raw = (obj.get("payload") or "").strip()
        try:
            decoded = json.loads(raw)
        except Exception:
            decoded = raw
        payload = decoded.strip() if isinstance(decoded, str) else raw
        action, _, raw_id = payload.partition(":")
        report_id = to_int(raw_id)
        admin_id = to_int(obj.get("user_id"))
        peer_id = to_int(obj.get("peer_id")) or CHAT_REPLY
        logger.info(
            "кнопка | payload %r | user %s | peer %s | report %s",
            raw,
            admin_id,
            peer_id,
            report_id,
        )
        if not report_id or action not in ("rep", "rpc"):
            logger.warning("неизвестная кнопка: %r", raw)
            return
        report = await self.db.get_report(report_id)
        if report is None:
            logger.warning("репорт %s не найден", report_id)
            return
        if action == "rep":
            await self.take_report(peer_id, admin_id, report)
        else:
            await self.cancel_report(peer_id, admin_id, report)

    async def take_command(self, peer_id: int, admin_id: int, report_id: int, cmid: int) -> None:
        report = await self.db.get_report(report_id)
        if report is None or report["status"] != "new":
            return
        await self.vk.delete(peer_id, cmid)
        await self.take_report(peer_id, admin_id, report)

    async def cancel_command(self, peer_id: int, admin_id: int, report_id: int, cmid: int) -> None:
        report = await self.db.get_report(report_id)
        if report is None or report["status"] != "taken":
            return
        await self.vk.delete(peer_id, cmid)
        await self.cancel_report(peer_id, admin_id, report)

    async def take_report(self, peer_id: int, admin_id: int, report: asyncpg.Record) -> None:
        if report["status"] != "new":
            return
        if report["message_cmid"]:
            await self.vk.delete(peer_id, to_int(report["message_cmid"]))
        text = (
            f"👮 {await self.vk.mention(admin_id)}, вы взялись отвечать на репорт"
            f" от {await self.vk.mention(to_int(report['author_id']))}"
            f"\nЧтобы ответить на жалобу, ответьте на это сообщение через реплай."
        )
        prompt_cmid = await self.vk.send(
            peer_id,
            text,
            keyboard=build_keyboard(callback_button("Отмена", f"rpc:{to_int(report['id'])}")),
        )
        await self.db.take_report(to_int(report["id"]), prompt_cmid, admin_id)

    async def cancel_report(self, peer_id: int, admin_id: int, report: asyncpg.Record) -> None:
        if report["status"] != "taken":
            return
        if report["prompt_cmid"]:
            await self.vk.delete(peer_id, to_int(report["prompt_cmid"]))
        text = (
            f"↩️ Администратор отказался браться за репорт"
            f" от {await self.vk.mention(to_int(report['author_id']))}"
            f"\n\n{await self.report_body(to_int(report['id']), to_int(report['author_id']), to_int(report['violator_id']), report['reason'] or '', report['reply_text'] or '', report['attachments'] or '')}"
        )
        message_cmid = await self.vk.send(
            peer_id,
            text,
            keyboard=build_keyboard(callback_button("Ответить", f"rep:{to_int(report['id'])}")),
        )
        await self.db.cancel_report(to_int(report["id"]), message_cmid)

    async def mute_user(self, peer_id: int, admin_id: int, cmid: int, rest: str) -> None:
        target_id, rest = extract_target(rest)
        seconds, rest = extract_duration(rest)
        reason = clean(rest) or DEFAULT_MUTE_REASON
        if not target_id:
            await self.vk.send(
                peer_id,
                "⚠️ Не указан пользователь.\nФормат:\nmuterep @user срок\nПричина\n"
                "Пример:\nmuterep @durov 10 минут\nОскорбления",
                reply_cmid=cmid,
            )
            return
        if not seconds:
            await self.vk.send(
                peer_id,
                "⚠️ Не указан срок. Сроки: 30 секунд, 10 минут, 2 часа, 10 дней.",
                reply_cmid=cmid,
            )
            return
        until_ts = now_ts() + seconds
        await self.db.set_mute(target_id, admin_id, reason, until_ts)
        removed, topics = await self.purge_board(target_id)
        target_name = await self.vk.mention(target_id)
        await self.vk.send(
            peer_id,
            f"🔇 Вы успешно выдали мут репорта {target_name} на {humanize(seconds)}"
            f"\nПричина: {reason}",
            reply_cmid=cmid,
        )
        await self.broadcast(
            f"👮 {await self.vk.mention(admin_id)} выдал мут репорта {target_name}"
            f" на {humanize(seconds)}\nПричина: {reason}"
        )

    async def purge_board(self, user_id: int) -> tuple[int, int]:
        rows = await self.db.board_by_user(user_id)
        by_topic: dict[int, list[int]] = {}
        for row in rows:
            by_topic.setdefault(to_int(row["topic_id"]), []).append(to_int(row["comment_id"]))
        removed = 0
        for topic, cids in by_topic.items():
            for cid in cids:
                if await self.vk.delete_board_comment(topic, cid):
                    removed += 1
            await self.db.forget_board(user_id, topic, cids)
        if removed or by_topic:
            logger.info(
                "мут %s: удалено комментариев %s в обсуждениях %s",
                user_id,
                removed,
                len(by_topic),
            )
        return removed, len(by_topic)

    async def on_board_comment(self, obj: dict) -> None:
        topic_id = to_int(obj.get("topic_id"))
        comment_id = to_int(obj.get("id"))
        user_id = to_int(obj.get("from_id"))
        if not topic_id or not comment_id or user_id <= 0:
            return
        if await self.db.get_mute(user_id) is not None:
            await self.vk.delete_board_comment(topic_id, comment_id)
            logger.info(
                "мут %s: снесён комментарий %s в обсуждении %s",
                user_id,
                comment_id,
                topic_id,
            )
            return
        await self.db.save_board_seen(topic_id, comment_id, user_id)

    async def unmute_user(self, peer_id: int, admin_id: int, cmid: int, rest: str) -> None:
        target_id, _ = extract_target(rest)
        if not target_id:
            await self.vk.send(
                peer_id, "⚠️ Формат: unmuterep @user", reply_cmid=cmid
            )
            return
        await self.db.delete_mute(target_id)
        target_name = await self.vk.mention(target_id)
        await self.vk.send(
            peer_id,
            f"✅ Вы успешно сняли блокировку репорта для {target_name}",
            reply_cmid=cmid,
        )
        await self.broadcast(
            f"👮 {await self.vk.mention(admin_id)} снял мут репорта с {target_name}."
        )

    async def broadcast(self, text: str) -> None:
        for chat_id in sorted(READ_CHATS):
            try:
                await self.vk.send(chat_id, text)
            except Exception as exc:
                logger.warning("Не удалось отправить в чат %s: %s", chat_id, exc)

    async def keep_alive(self) -> None:
        if not PING_URL:
            logger.info("PUBLIC_URL не задан, самопинг отключён")
            return
        url = f"{PING_URL}/ping"
        while True:
            try:
                async with self.vk.session.get(url, timeout=20) as response:
                    await response.read()
                    logger.info("keep-alive %s -> %s", url, response.status)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("keep-alive не удался (%s)", exc)
            await asyncio.sleep(PING_INTERVAL)

    async def watch_mutes(self) -> None:
        while True:
            try:
                for row in await self.db.expired_mutes():
                    user_id = to_int(row["user_id"])
                    await self.db.delete_mute(user_id)
                    await self.broadcast(
                        f"⏳ {await self.vk.mention(user_id)}, срок блокировки репорта истёк."
                        "\nВы снова можете отправлять сообщения и жалобы."
                    )
            except Exception as exc:
                logger.exception("Ошибка проверки мутов: %s", exc)
            await asyncio.sleep(WATCH_INTERVAL)


class CallbackServer:
    def __init__(self, bot: ReportBot) -> None:
        self.bot = bot
        self.tasks: set[asyncio.Task] = set()
        self._warned = False

    async def confirmation(self, request: web.Request) -> web.Response:
        return web.Response(text=CONFIRMATION_CODE)

    async def ping(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def events(self, request: web.Request) -> web.Response:
        try:
            event = await request.json()
        except Exception:
            return web.Response(text=CONFIRMATION_CODE)
        if not is_event(event):
            return web.Response(text=CONFIRMATION_CODE)
        secret = request.query.get("secret", "")
        if secret and secret != CONFIRMATION_CODE:
            logger.warning("Событие с неверным секретом отброшено")
            return web.Response(text="ok")
        if not secret and not self._warned:
            self._warned = True
            logger.warning(
                "В URL колбэка нет параметра ?secret — события принимаются без проверки"
            )
        task = asyncio.create_task(self.process(event))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return web.Response(text="ok")

    async def process(self, event: dict) -> None:
        try:
            await self.bot.handle_event(event or {})
        except Exception as exc:
            logger.exception("Ошибка обработки события: %s", exc)

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ping", self.ping)
        app.router.add_route("*", "/", self.events)
        app.router.add_route("*", "/{path:.*}", self.events)
        return app


def validate() -> None:
    missing = [
        name
        for name, value in (
            ("GROUP_TOKEN", TOKEN),
            ("GROUP_ID", GROUP_ID),
            ("DEV_ID", DEV_ID),
            ("CONFIRMATION_CODE", CONFIRMATION_CODE),
            ("CHAT_REPLY", CHAT_REPLY),
            ("CHAT_READ", READ_CHATS),
            ("DATABASE", DATABASE_URL),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Не заполнены переменные: {', '.join(missing)}")


async def main() -> None:
    validate()
    vk = VK(TOKEN, GROUP_ID)
    db = Database(DATABASE_URL)
    try:
        await vk.start()
        delay = 5
        while True:
            try:
                await db.connect()
                break
            except Exception as exc:
                logger.warning("БД недоступна (%s), повтор через %s с", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        bot = ReportBot(vk, db)
        server = CallbackServer(bot)
        app = server.build_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        watcher = asyncio.create_task(bot.watch_mutes())
        pinger = asyncio.create_task(bot.keep_alive())
        logger.info("Бот запущен, порт %s", PORT)
        try:
            await asyncio.Event().wait()
        finally:
            watcher.cancel()
            pinger.cancel()
            await runner.cleanup()
    finally:
        await db.close()
        await vk.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
