from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from .errors import AuthenticationError, BoardUnavailableError, require_module
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
                   c.allowed_start, c.allowed_end
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

        return User(user_id=int(row["user_id"]), username=str(row["username"]))

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
            where_sql = "WHERE status = 'available'"
        select_sql = f"""
            SELECT fpga_name, total_port, twin_port, jtag_filter, vcom_name, com_name, IP, result
            FROM fpga_boards
            {where_sql}
            ORDER BY RAND()
            LIMIT 1
        """
        update_sql = "UPDATE fpga_boards SET status = 'in_use' WHERE fpga_name = %s AND status = 'available'"
        for _ in range(5):
            with self.connect() as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(select_sql, params)
                        row = cur.fetchone()
                        if not row:
                            if fpga_name:
                                raise BoardUnavailableError(f"FPGA board not found: {fpga_name}")
                            if force:
                                raise BoardUnavailableError("no FPGA board found")
                            raise BoardUnavailableError("no available FPGA board")
                        board = Board.from_row(row)
                        if force or fpga_name:
                            conn.commit()
                            return board
                        cur.execute(update_sql, (board.fpga_name,))
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
                cur.execute("UPDATE fpga_boards SET status = 'available' WHERE fpga_name = %s", (fpga_name,))
            conn.commit()

    def mark_board_in_use(self, fpga_name: str) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE fpga_boards SET status = 'in_use' WHERE fpga_name = %s", (fpga_name,))
            conn.commit()

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
