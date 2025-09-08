"""
LLM vs Random test script

- Runs battles between the LLMPlayer and a RandomPlayer using the poke-env framework.
- Designed to test LLM-driven decision making via the OpenAI API, with future plans
  to migrate toward local inference instead of API calls.
- Supports command-line arguments (--battles, --format, --host, --port, etc.) to configure battles.
- Can run on both local and public Showdown servers.
- Optionally prints or opens a spectator URL for live battle observation.
"""

# ruff: noqa
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from poke_env import RandomPlayer
from poke_env.ps_client.server_configuration import (
    LocalhostServerConfiguration,
    ServerConfiguration,
)

from agent.llm_player import LLMPlayer

# .env 로드
load_dotenv(dotenv_path=str(Path(__file__).resolve().parents[2] / ".env"))


async def _announce_room(me: LLMPlayer, host: str, port: int, open_browser: bool):
    """
    poke-env 0.10 호환:
    - 예전엔 Player.current_battles 가 있었지만, 현재는 내부 dict인 _battles 를 사용.
    - 둘 다 안전하게 체크해서 첫 번째 배틀 room id(tag)로 관전 링크 출력.
    """
    import webbrowser

    def _battle_dict():
        for name in ("current_battles", "_battles", "battles"):
            d = getattr(me, name, None)
            if isinstance(d, dict) and d:
                return d
        return None

    # 공개 서버/로컬 서버에 따라 베이스 URL 결정
    if host in ("localhost", "127.0.0.1"):
        base_url = f"http://{host}:{port}"
    else:
        base_url = "https://play.pokemonshowdown.com"

    for _ in range(200):
        d = _battle_dict()
        if d:
            roomid = next(iter(d.keys()))
            url = f"{base_url}/{roomid}"
            print(f"[WATCH] Spectate here → {url}")
            if open_browser:
                webbrowser.open(url)
            break
        await asyncio.sleep(0.1)


async def run(
    n_battles=1,
    fmt="gen9randombattle",
    host="localhost",
    port=8000,
    debug_llm=False,
    open_browser=False,
):
    # 서버 설정
    if host in ("localhost", "127.0.0.1") and port == 8000:
        server = LocalhostServerConfiguration
    else:
        ws_url = f"ws://{host}:{port}/showdown/websocket"
        auth_url = "https://play.pokemonshowdown.com/action.php?"
        server = ServerConfiguration(ws_url, auth_url)

    me = LLMPlayer(
        battle_format=fmt,
        max_concurrent_battles=1,
        server_configuration=server,
        start_timer_on_battle_start=True,
        debug=debug_llm,
    )
    opp = RandomPlayer(
        battle_format=fmt,
        max_concurrent_battles=1,
        server_configuration=server,
    )
    print(f"Start LLM({me.username}) vs Random({opp.username}) - {fmt}")
    asyncio.create_task(_announce_room(me, host, port, open_browser))
    await me.battle_against(opp, n_battles=n_battles)
    print(f"Done. LLM won {me.n_won_battles} / lost {me.n_lost_battles}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--battles", type=int, default=1)
    ap.add_argument("--format", default="gen9randombattle")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--debug-llm", action="store_true", help="LLM 상세 프롬프트/출력 보기 (full 모드와 함께 추천)")
    ap.add_argument("--open", action="store_true", help="관전 URL 자동으로 브라우저 열기")
    ap.add_argument("--log-mode", choices=["none", "compact", "full"], default=None)
    ap.add_argument("--quiet-lib-logs", action="store_true")

    args = ap.parse_args()

    if args.log_mode:
        os.environ["LLM_LOG_MODE"] = args.log_mode
    if args.quiet_lib_logs:
        os.environ["QUIET_LIB_LOGS"] = "1"

    asyncio.run(
        run(
            n_battles=args.battles,
            fmt=args.format,
            host=args.host,
            port=args.port,
            debug_llm=args.debug_llm,
            open_browser=args.open,
        )
    )
