from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from local2spoti.db import connect, init_schema
from local2spoti.token_refresh import refresh_if_expiring


async def test_refreshes_when_within_threshold(tmp_path):
    db = tmp_path / "t.db"
    async with connect(db) as conn:
        await init_schema(conn)
        soon = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
        await conn.execute(
            """INSERT INTO auth_token (key, access_token, refresh_token,
               expires_at, scope, user_id)
               VALUES ('spotify','old','rt',?,'x','u')""",
            (soon,),
        )
        await conn.commit()
        with patch(
            "local2spoti.token_refresh.refresh_token",
            new=AsyncMock(
                return_value={
                    "access_token": "new",
                    "expires_in": 3600,
                    "scope": "x",
                    "token_type": "Bearer",
                }
            ),
        ):
            refreshed = await refresh_if_expiring(
                conn=conn,
                client_id="cid",
                threshold_seconds=300,
            )
        assert refreshed is True
        cur = await conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
        (tok,) = await cur.fetchone()
        assert tok == "new"


async def test_skips_when_not_expiring(tmp_path):
    db = tmp_path / "t.db"
    async with connect(db) as conn:
        await init_schema(conn)
        far = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        await conn.execute(
            """INSERT INTO auth_token (key, access_token, refresh_token,
               expires_at, scope, user_id)
               VALUES ('spotify','keep','rt',?,'x','u')""",
            (far,),
        )
        await conn.commit()
        refreshed = await refresh_if_expiring(
            conn=conn,
            client_id="cid",
            threshold_seconds=300,
        )
    assert refreshed is False
