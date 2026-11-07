"""Discord/oyunda "Alice" denmeden gelen devam cümlesi ALICE'e mi söylendi? (Jev ile)

Kullanıcı kendi sesiyle arkadaşlarına da konuşur; ses izi bunu ayıramaz, canlı model de "sana söylenmediyse sus"
talimatına uymuyordu ("Bilemiyorum", "Ne olacaksa?" gibi arkadaşa söylenen cümlelere cevap verdi, 7 Kasım).
Canlı model cevap üretmeden önce döküm Jev'e sorulur; karar koddadır:

  p >= ADDRESSED   -> cevap ver
  p <  ADDRESSED   -> sus (oturum sessizce kapanır)
  Jev'e ulaşılamadı -> cevap ver (eski davranış; konuşma kilitlenmesin) ve günlüğe yaz
"""

from __future__ import annotations

from typing import Any, Callable

from alice.decider import Noul

SETTING = ("The user is playing an online game while talking with friends on Discord voice chat. ALICE, the user's "
           "voice assistant, hears everything the user says into the microphone. ALICE just finished speaking "
           "(`alice_last_reply`), and the user said `utterance` within a few seconds, without saying ALICE's name.")

QUESTION = Noul(
    "Is `utterance` said to ALICE?",
    {"true": "The user answers ALICE's last reply or question, asks ALICE something, or gives ALICE a task.",
     "false": "The user is talking to friends or to themselves, for example about the game or what a teammate "
              "did, and the sentence does not follow from ALICE's last reply."})

# TypeSafe'in varsayılan "evet" eşiği. 0,3-0,7 arası "belli değil": Discord'da susmak, arkadaşa söylenen cümleye
# cevap vermekten iyidir. Denemede ALICE'e söylenenler 0,84-0,93, arkadaşa söylenenler 0,05-0,39 aldı.
ADDRESSED = 0.7


def build_state(utterance: str, alice_last_reply: str) -> dict[str, Any]:
    return {"setting": SETTING, "alice_last_reply": alice_last_reply.strip()[:600] or "(none)",
            "utterance": utterance.strip()[:600]}


def is_addressed(config, utterance: str, alice_last_reply: str = "", engine: Any = None,
                 log: Callable[[str], None] = lambda s: None) -> tuple[bool, float | None]:
    """(cevap verilsin mi, olasılık). Olasılık None: Jev'e sorulamadı."""
    if not utterance.strip():
        return False, None
    if engine is None:
        from alice.providers.jev_route import _get_engine

        engine = _get_engine(config)
    if engine is None:
        return True, None
    try:
        d = engine.decide(build_state(utterance, alice_last_reply), {"addressed": QUESTION})
        p = float(d["addressed"].value)
    except Exception as e:  # noqa: BLE001 - hata bir cevap değildir: konuşma kilitlenmesin
        log(f"hitap kontrolü başarısız ({str(e)[:120]}), cevap veriliyor")
        return True, None
    return p >= ADDRESSED, p
