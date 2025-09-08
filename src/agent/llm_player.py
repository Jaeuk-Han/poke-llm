"""
LLMPlayer module

- Extends poke-env's Player class to implement an agent that selects actions
  (move/switch) in Pokémon battles using an LLM.
- Currently integrates with the OpenAI API for testing purposes, but will be
  extended to support local inference in the future.
- Sends the current battle state and candidate actions to the LLM, then parses
  and validates its JSON response to execute the decision.
- Provides safe fallbacks (e.g., expected damage heuristic) when the LLM output
  is invalid or unavailable.
"""

# ruff: noqa
from __future__ import annotations

import json
import os
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# poke-env 0.10: Player는 여기
from poke_env.player.player import Player

# 타입 힌트를 위해 (런타임 의존 없음)
try:
    from poke_env.battle.battle import Battle
    from poke_env.battle.move import Move
    from poke_env.battle.pokemon import Pokemon
except Exception:  # pragma: no cover
    Battle = Any  # type: ignore
    Move = Any  # type: ignore
    Pokemon = Any  # type: ignore

# OpenAI 클라이언트 (키 없으면 None)
try:
    from openai import OpenAI  # >= 1.x
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


# =========================
# Env / Logging Config
# =========================

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(dotenv_path=str(ROOT / ".env"))  # 프로젝트 루트 .env 우선 로드

LOG_MODE = os.getenv("LLM_LOG_MODE", "none")  # none | compact | full
TRACE_FILE = os.getenv("LLM_TRACE_FILE")  # jsonl 경로
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")

# 기록 파일 디렉토리 자동 생성
if TRACE_FILE:
    Path(TRACE_FILE).parent.mkdir(parents=True, exist_ok=True)


def _print_full(*a):
    if LOG_MODE == "full":
        print(*a)


def _print_compact(*a):
    if LOG_MODE in ("compact", "full"):
        print(*a)


def _trace(event: str, **payload):
    """한 줄 JSONL로 디스크에 기록 (ON/OFF: LLM_TRACE_FILE)."""
    if not TRACE_FILE:
        return
    rec = {"event": event, **payload}
    try:
        with open(TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# =========================
# 데이터 포맷 정의
# =========================

@dataclass
class RowMove:
    kind: str  # "move"
    index: int
    id: str
    name: str
    type: str
    base_power: int
    accuracy: float
    priority: int
    category: str
    pp: int
    is_stab: bool


@dataclass
class RowSwitch:
    kind: str  # "switch"
    index: int
    species: str
    types: List[str]
    hp_pct: int
    status: Optional[str]


def _safe_types(obj) -> List[str]:
    """poke-env Type enum -> 대문자 문자열 리스트."""
    try:
        return [t.name for t in (obj.types or []) if hasattr(t, "name")]
    except Exception:
        return []


def _hp_pct(p: Pokemon) -> int:
    try:
        if p.current_hp is None or p.max_hp is None or p.max_hp == 0:
            return 100
        return max(0, min(100, int(round(100 * p.current_hp / p.max_hp))))
    except Exception:
        return 100


def _status(p: Pokemon) -> Optional[str]:
    try:
        return p.status.name if p.status else None
    except Exception:
        return None


def _move_accuracy(m: Move) -> float:
    """정확도 None/0 → 1.0으로 간주(쇼다운 특수값 대응)."""
    try:
        acc = m.accuracy
        if acc in (None, 0):
            return 1.0
        # poke-env는 1.0 스케일일 가능성이 큼 (간혹 100 scale도 만나므로 보정)
        return float(acc if acc <= 1 else acc / 100.0)
    except Exception:
        return 1.0


def _move_base_power(m: Move) -> int:
    try:
        bp = int(m.base_power or 0)
        return max(0, bp)
    except Exception:
        return 0


def _is_stab(move: Move, me_active: Optional[Pokemon]) -> bool:
    try:
        if not me_active or not move.type:
            return False
        my_types = _safe_types(me_active)
        return move.type.name in my_types
    except Exception:
        return False


def _category(m: Move) -> str:
    try:
        return m.damage_class.name if m.damage_class else "STATUS"
    except Exception:
        return "STATUS"


def _priority(m: Move) -> int:
    try:
        return int(m.priority or 0)
    except Exception:
        return 0


def _move_row(i: int, m: Move, me_active: Optional[Pokemon]) -> RowMove:
    return RowMove(
        kind="move",
        index=i,
        id=getattr(m, "id", getattr(m, "name", f"move{i}")).lower(),
        name=(getattr(m, "name", getattr(m, "id", f"move{i}")) or f"move{i}").lower(),
        type=(m.type.name if getattr(m, "type", None) else "UNKNOWN"),
        base_power=_move_base_power(m),
        accuracy=_move_accuracy(m),
        priority=_priority(m),
        category=_category(m),
        pp=int(getattr(m, "current_pp", getattr(m, "max_pp", 0)) or 0),
        is_stab=_is_stab(m, me_active),
    )


def _switch_row(i: int, p: Pokemon) -> RowSwitch:
    return RowSwitch(
        kind="switch",
        index=i,
        species=(getattr(p, "species", getattr(p, "name", f"p{i}")) or f"p{i}").lower(),
        types=_safe_types(p),
        hp_pct=_hp_pct(p),
        status=_status(p),
    )


def _score_move_row(r: RowMove) -> float:
    """간단한 기대대미지 점수: bp * acc * (1.5 if STAB). (효과抜群 계산은 생략)"""
    return (r.base_power or 0) * max(0.0, min(1.0, r.accuracy or 0.0)) * (1.5 if r.is_stab else 1.0)


def _extract_json(text: str) -> Dict[str, Any]:
    """
    LLM이 코드펜스/설명 포함해도 첫 JSON 오브젝트를 안정적으로 파싱.
    """
    text = (text or "").strip()
    # 1) 깔끔한 JSON만 온 경우
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # 2) 안에 JSON 섞여있는 경우: 여는 중괄호 위치마다 시도
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            ch = text[end]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start : end + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        pass
                    break
    raise ValueError("no_json_object_found")


def _validate_decision(dec: dict, rows: List[dict], state: dict) -> Tuple[bool, str]:
    if not isinstance(dec, dict):
        return False, "not_dict"
    act = dec.get("action")
    idx = dec.get("index")
    if act not in ("move", "switch", "force_switch"):
        return False, f"bad_action:{act}"
    if not isinstance(idx, int):
        return False, "index_not_int"

    n_moves = sum(1 for r in rows if r["kind"] == "move")
    n_switches = sum(1 for r in rows if r["kind"] == "switch")

    if state.get("force_switch") and act == "move":
        return False, "must_switch"

    if act in ("move", "force_switch"):
        if idx < 0 or idx >= n_moves:
            return False, "index_out_of_range_move"
    if act == "switch":
        if idx < 0 or idx >= n_switches:
            return False, "index_out_of_range_switch"
    return True, ""


# =========================
# LLM 플레이어
# =========================

class LLMPlayer(Player):
    """
    - LLM이 JSON으로 {"action":"move|switch|force_switch","index":0-based,"reason":"short"} 응답
    - 파싱 실패/스키마 불일치 → 안전한 폴백
    - TRACE_FILE 설정 시 JSONL 이벤트 기록
    """

    def __init__(self, model: Optional[str] = None, debug: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.model = model or OPENAI_MODEL
        self.debug = bool(debug)

        self._client = None
        if OPENAI_KEY and OpenAI is not None:
            try:
                self._client = OpenAI(api_key=OPENAI_KEY)
            except Exception:
                self._client = None

    # -------- 핵심: 선택 --------
    def choose_move(self, battle: Battle):
        me_active: Optional[Pokemon] = getattr(battle, "active_pokemon", None)
        opp_active: Optional[Pokemon] = getattr(battle, "opponent_active_pokemon", None)

        # 후보 구성
        moves = list(getattr(battle, "available_moves", []) or [])
        switches = list(getattr(battle, "available_switches", []) or [])

        move_rows: List[RowMove] = [
            _move_row(i, m, me_active) for i, m in enumerate(moves)
        ]
        switch_rows: List[RowSwitch] = [
            _switch_row(i, p) for i, p in enumerate(switches)
        ]

        # 상태 요약
        def dump_mon(p: Optional[Pokemon]) -> Dict[str, Any]:
            if not p:
                return {"species": "unknown", "types": [], "hp_pct": None, "status": None}
            return {
                "species": (getattr(p, "species", getattr(p, "name", "unknown")) or "unknown").lower(),
                "types": _safe_types(p),
                "hp_pct": _hp_pct(p),
                "status": _status(p),
            }

        weather = getattr(battle, "weather", None)
        terrain = getattr(battle, "terrain", None) if hasattr(battle, "terrain") else None

        state = {
            "turn": int(getattr(battle, "turn", 0) or 0),
            "force_switch": bool(getattr(battle, "force_switch", False)),
            "my_active": dump_mon(me_active),
            "opp_active": dump_mon(opp_active),
            "weather": weather.name if getattr(weather, "name", None) else "none",
            "terrain": terrain.name if getattr(terrain, "name", None) else "none",
        }

        # 화면 출력 (compact)
        if LOG_MODE in ("compact", "full"):
            if state["turn"] <= 1 or state["force_switch"]:
                print()
            _print_compact(f"--- TURN {state['turn']} ---")
            _print_compact(
                f"My: {state['my_active']} Opp: {state['opp_active']} "
                f"Weather: {state['weather']} Terrain: {state['terrain']}"
            )

        # 테이블 출력
        if LOG_MODE in ("compact", "full"):
            try:
                from rich.table import Table
                from rich.console import Console

                table = Table("kind", "idx", "name/species", "type(s)", "bp", "acc", "stab")
                for r in move_rows:
                    table.add_row("move", str(r.index), r.name, r.type, str(r.base_power), f"{r.accuracy:.1f}", "✓" if r.is_stab else "")
                for r in switch_rows:
                    ts = "/".join(r.types) if r.types else "-"
                    table.add_row("switch", str(r.index), r.species, ts, "-", "-", "-")
                Console().print(table)
            except Exception:
                pass

        # LLM 의사결정 → 실패 시 폴백
        rows_for_llm: List[Dict[str, Any]] = [asdict(r) for r in move_rows] + [asdict(r) for r in switch_rows]

        try:
            dec = self._llm_decide(state, rows_for_llm)
        except Exception as e:
            # 폴백: 강제 교대 상황이면 첫 스위치, 아니면 기대대미지 최대 무브
            if state["force_switch"] and switch_rows:
                dec = {"action": "switch", "index": 0, "reason": f"fallback:force_switch ({e})"}
            elif move_rows:
                best_i = max(range(len(move_rows)), key=lambda i: _score_move_row(move_rows[i]))
                dec = {"action": "move", "index": best_i, "reason": f"fallback:expected-damage ({e})"}
            else:
                # 정말 아무것도 없으면 대충
                dec = {"action": "move", "index": 0, "reason": f"fallback:default ({e})"}
            _trace("fallback_exception", turn=state.get("turn"), error=str(e), dec=dec, state=state)

        # 결정 출력
        if LOG_MODE in ("compact", "full"):
            if dec["action"] == "move":
                r = move_rows[dec["index"]] if 0 <= dec["index"] < len(move_rows) else None
                name = r.name if r else "?"
                _print_compact(f"[DECIDE] MOVE idx={dec['index']} ({name}) | reason={dec.get('reason','')}")
            else:
                r = switch_rows[dec["index"]] if 0 <= dec["index"] < len(switch_rows) else None
                name = (r.species if r else "?")
                _print_compact(f"[DECIDE] SWITCH idx={dec['index']} ({name}) | reason={dec.get('reason','')}")

        # poke-env 주문 생성
        if dec["action"] in ("move", "force_switch") and move_rows:
            i = max(0, min(dec["index"], len(moves) - 1))
            return self.create_order(moves[i])

        if dec["action"] == "switch" and switch_rows:
            i = max(0, min(dec["index"], len(switches) - 1))
            return self.create_order(switches[i])

        # 마지막 안전장치
        if moves:
            return self.create_order(moves[0])
        return self.choose_random_move(battle)

    # -------- LLM 호출+검증 --------
    def _llm_decide(self, state: dict, rows: List[dict]) -> dict:
        # 테스트용: 일부러 나쁜 출력 유도
        if os.getenv("LLM_FORCE_BAD_OUTPUT") == "1":
            if self.debug and LOG_MODE == "full":
                print("[LLM] (forced) RAW OUTPUT: MOVE 1")
            _trace("llm_forced_bad", turn=state.get("turn"))
            raise ValueError("forced_bad_output")

        # 키 없으면 폴백 루트로 위에서 처리
        if not self._client:
            # 여기선 raise해서 상위 폴백 경로로 보냄
            raise RuntimeError("no_openai_client")

        sys_prompt = (
            "You are a Pokémon battle assistant. Choose exactly one action from the candidates.\n"
            "Prefer higher expected damage ≈ base_power * accuracy * (1.5 if is_stab else 1). "
            "If state.force_switch is true, you MUST choose switch/force_switch.\n"
            'Reply with ONLY this JSON: {"action":"move|switch|force_switch","index":0-based integer,"reason":"short"}'
        )
        user_msg = {"state": state, "candidates": rows}

        if self.debug and LOG_MODE == "full":
            print("[LLM] === PROMPT ===")
            try:
                print("You are a Pokémon battle assistant. Choose exactly one action from the candidates. Reply with a single JSON object ONLY: {\"action\":\"move|switch|force_switch\",\"index\":0-based integer,\"reason\":\"short\"}")
                print(json.dumps(user_msg, ensure_ascii=False, indent=2))
            except Exception:
                print(user_msg)
            print("[LLM] ===============")

        # 실제 호출
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(user_msg, ensure_ascii=False)},
            ],
            temperature=0.15,
            max_tokens=128,
        )
        raw = resp.choices[0].message.content or ""

        if self.debug and LOG_MODE == "full":
            print("[LLM] RAW OUTPUT:", raw)

        # 파싱
        try:
            dec = _extract_json(raw)
        except Exception as e:
            _trace("llm_bad_json", turn=state.get("turn"), raw=raw, error=str(e), state=state)
            raise

        # 스키마 검증
        ok, err = _validate_decision(dec, rows, state)
        if not ok:
            _trace("llm_bad_decision", turn=state.get("turn"), raw=raw, parsed=dec, error=err, state=state, rows=rows)
            raise ValueError(err)

        _trace("llm_ok", turn=state.get("turn"), raw=raw, parsed=dec, state=state, rows=rows)
        return dec
