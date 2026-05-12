from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from .errors import (
    AuthenticationError,
    BoardUnavailableError,
    JydClientError,
    QuotaExceededError,
    require_module,
)
from .models import Board, User


class Database:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pymysql = require_module("pymysql", "pip install -r requirements.txt")

    @contextmanager
    def connect(self) -> Iterator[object]:
        conn = self.pymysql.connect(
            host=self.cfg["host"],
            port=int(self.cfg["port"]),
            user=self.cfg["user"],
            password=self.cfg["password"],
            database=self.cfg["database"],
            charset=self.cfg.get("charset", "utf8mb4"),
            cursorclass=self.pymysql.cursors.DictCursor,
            autocommit=False,
        )
        try:
            yield conn
        finally:
            conn.close()

    def authenticate(self, username: str, password: str) -> User:
        sql = """
            SELECT u.user_id, u.password, u.user_status, u.username,
                   c.allowed_start, c.allowed_end, u.used_times, u.limit_times
            FROM users u
            JOIN classes c ON u.class_id = c.class_id
            WHERE u.username=%s
        """
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (username,))
                row = cur.fetchone()

        if not row:
            raise AuthenticationError("user not found")
        if str(row.get("password")) != str(password):
            raise AuthenticationError("password error")
        if str(row.get("user_status", "")).lower() not in {"allowed", "1", "true"}:
            raise AuthenticationError(f"user status denied: {row.get('user_status')}")

        now = datetime.now()
        start = _to_datetime(row.get("allowed_start"))
        end = _to_datetime(row.get("allowed_end"))
        if start and now < start:
            raise AuthenticationError(f"login not allowed before {start}")
        if end and now > end:
            raise AuthenticationError(f"login not allowed after {end}")

        user = User(
            user_id=int(row["user_id"]),
            username=str(row["username"]),
            used_times=_optional_int(row.get("used_times")),
            limit_times=_optional_int(row.get("limit_times")),
        )
        if _quota_exhausted(user.used_times, user.limit_times):
            raise QuotaExceededError(_format_quota_error(user.used_times, user.limit_times))
        return user

    def get_user_usage(self, username_or_user_id: str | int) -> User:
        if isinstance(username_or_user_id, int) or str(username_or_user_id).isdigit():
            where_sql = "user_id = %s"
            value = int(username_or_user_id)
        else:
            where_sql = "username = %s"
            value = str(username_or_user_id)
        sql = f"""
            SELECT user_id, username, used_times, limit_times
            FROM users
            WHERE {where_sql}
        """
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (value,))
                row = cur.fetchone()
        if not row:
            raise AuthenticationError(f"user not found: {username_or_user_id}")
        return User(
            user_id=int(row["user_id"]),
            username=str(row["username"]),
            used_times=_optional_int(row.get("used_times")),
            limit_times=_optional_int(row.get("limit_times")),
        )

    def increment_usage_count(self, user_id: int) -> User:
        select_sql = """
            SELECT user_id, username, used_times, limit_times
            FROM users
            WHERE user_id = %s
            FOR UPDATE
        """
        update_sql = "UPDATE users SET used_times = COALESCE(used_times, 0) + 1 WHERE user_id = %s"
        with self.connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(select_sql, (user_id,))
                    row = cur.fetchone()
                    if not row:
                        raise AuthenticationError(f"user not found: {user_id}")
                    used_times = _optional_int(row.get("used_times")) or 0
                    limit_times = _optional_int(row.get("limit_times"))
                    if _quota_exhausted(used_times, limit_times):
                        raise QuotaExceededError(_format_quota_error(used_times, limit_times))
                    cur.execute(update_sql, (user_id,))
                    row["used_times"] = used_times + 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return User(
            user_id=int(row["user_id"]),
            username=str(row["username"]),
            used_times=_optional_int(row.get("used_times")),
            limit_times=_optional_int(row.get("limit_times")),
        )

    def set_user_usage(
        self,
        username_or_user_id: str | int,
        *,
        limit_times: int | None = None,
        used_times: int | None = None,
    ) -> User:
        if limit_times is None and used_times is None:
            raise JydClientError("at least one of --limit-times or --used-times must be provided")
        if limit_times is not None and limit_times < 0:
            raise JydClientError("--limit-times must be >= 0")
        if used_times is not None and used_times < 0:
            raise JydClientError("--used-times must be >= 0")
        user = self.get_user_usage(username_or_user_id)
        assignments = []
        params: list[int] = []
        if limit_times is not None:
            assignments.append("limit_times = %s")
            params.append(limit_times)
        if used_times is not None:
            assignments.append("used_times = %s")
            params.append(used_times)
        params.append(user.user_id)
        sql = f"UPDATE users SET {', '.join(assignments)} WHERE user_id = %s"
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
            conn.commit()
        return self.get_user_usage(user.user_id)

    def list_boards(self) -> list[Board]:
        sql = """
            SELECT fpga_name, total_port, twin_port, jtag_filter, vcom_name, com_name, IP, result, status
            FROM fpga_boards
            ORDER BY fpga_name
        """
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        return [Board.from_row(row) for row in rows]

    def allocate_board(self, force: bool = False, fpga_name: str | None = None) -> Board:
        where_sql = ""
        params: tuple[str, ...] = ()
        if fpga_name:
            where_sql = "WHERE fpga_name = %s"
            params = (fpga_name,)
        elif not force:
            stale_minutes = int(self.cfg.get("stale_board_minutes", 3))
            where_sql = (
                "WHERE status = 'available' "
                "OR (status = 'in_use' AND last_heartbeat < "
                f"DATE_SUB(NOW(), INTERVAL {stale_minutes} MINUTE))"
            )
        select_sql = f"""
            SELECT fpga_name, total_port, twin_port, jtag_filter, vcom_name, com_name, IP, result
            FROM fpga_boards
            {where_sql}
            ORDER BY status DESC, last_heartbeat ASC, RAND()
            LIMIT 1
        """
        fallback_select_sql = f"""
            SELECT fpga_name, total_port, twin_port, jtag_filter, vcom_name, com_name, IP, result
            FROM fpga_boards
            {"WHERE fpga_name = %s" if fpga_name else "" if force else "WHERE status = 'available'"}
            ORDER BY RAND()
            LIMIT 1
        """
        update_sql = (
            "UPDATE fpga_boards SET status = 'in_use', last_heartbeat = NOW() "
            "WHERE fpga_name = %s AND (status = 'available' OR last_heartbeat < "
            f"DATE_SUB(NOW(), INTERVAL {int(self.cfg.get('stale_board_minutes', 3))} MINUTE))"
        )
        fallback_update_sql = (
            "UPDATE fpga_boards SET status = 'in_use' WHERE fpga_name = %s AND status = 'available'"
        )
        force_update_sql = "UPDATE fpga_boards SET status = 'in_use', last_heartbeat = NOW() WHERE fpga_name = %s"
        fallback_force_update_sql = "UPDATE fpga_boards SET status = 'in_use' WHERE fpga_name = %s"
        for _ in range(5):
            with self.connect() as conn:
                try:
                    with conn.cursor() as cur:
                        using_heartbeat = True
                        try:
                            cur.execute(select_sql, params)
                        except Exception as exc:
                            if not _is_unknown_column_error(exc, "last_heartbeat"):
                                raise
                            using_heartbeat = False
                            cur.execute(fallback_select_sql, params)
                        row = cur.fetchone()
                        if not row:
                            if fpga_name:
                                raise BoardUnavailableError(f"FPGA board not found: {fpga_name}")
                            if force:
                                raise BoardUnavailableError("no FPGA board found")
                            raise BoardUnavailableError("no available FPGA board")
                        board = Board.from_row(row)
                        if force or fpga_name:
                            cur.execute(
                                force_update_sql if using_heartbeat else fallback_force_update_sql,
                                (board.fpga_name,),
                            )
                            conn.commit()
                            return board
                        cur.execute(update_sql if using_heartbeat else fallback_update_sql, (board.fpga_name,))
                        if cur.rowcount != 1:
                            conn.rollback()
                            continue
                    conn.commit()
                    return board
                except Exception:
                    conn.rollback()
                    raise
        raise BoardUnavailableError("available FPGA board was claimed concurrently")

    def release_board(self, fpga_name: str) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "UPDATE fpga_boards SET status = 'available', last_heartbeat = NULL WHERE fpga_name = %s",
                        (fpga_name,),
                    )
                except Exception as exc:
                    if not _is_unknown_column_error(exc, "last_heartbeat"):
                        raise
                    cur.execute("UPDATE fpga_boards SET status = 'available' WHERE fpga_name = %s", (fpga_name,))
            conn.commit()

    def mark_board_in_use(self, fpga_name: str) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "UPDATE fpga_boards SET status = 'in_use', last_heartbeat = NOW() WHERE fpga_name = %s",
                        (fpga_name,),
                    )
                except Exception as exc:
                    if not _is_unknown_column_error(exc, "last_heartbeat"):
                        raise
                    cur.execute("UPDATE fpga_boards SET status = 'in_use' WHERE fpga_name = %s", (fpga_name,))
            conn.commit()

    def update_board_heartbeat(self, fpga_name: str) -> bool:
        with self.connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("UPDATE fpga_boards SET last_heartbeat = NOW() WHERE fpga_name = %s", (fpga_name,))
                except Exception as exc:
                    if _is_unknown_column_error(exc, "last_heartbeat"):
                        conn.rollback()
                        return False
                    raise
            conn.commit()
        return True

    def reset_all_boards_available(self) -> int:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE fpga_boards SET status = 'available'")
                affected = cur.rowcount
            conn.commit()
        return int(affected)

    def save_result(self, fpga_name: str, result_str: str) -> None:
        create_sql = """
            CREATE TABLE IF NOT EXISTS experiment_results (
                id INT AUTO_INCREMENT PRIMARY KEY,
                fpga_name VARCHAR(50),
                display_result VARCHAR(20),
                record_time DATETIME
            )
        """
        insert_sql = (
            "INSERT INTO experiment_results (fpga_name, display_result, record_time) "
            "VALUES (%s, %s, %s)"
        )
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(create_sql)
                cur.execute(insert_sql, (fpga_name, result_str, datetime.now()))
            conn.commit()


def _to_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                pass
    return None


def _optional_int(value):
    if value is None or value == "":
        return None
    return int(value)


def _quota_exhausted(used_times: int | None, limit_times: int | None) -> bool:
    if limit_times is None or limit_times <= 0:
        return False
    return (used_times or 0) >= limit_times


def _format_quota_error(used_times: int | None, limit_times: int | None) -> str:
    return f"usage quota exhausted: used_times={used_times or 0} limit_times={limit_times}"


def _is_unknown_column_error(exc: Exception, column: str) -> bool:
    text = str(exc).lower()
    return "unknown column" in text and column.lower() in text
