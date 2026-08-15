import re

delivery_annotation_version = 1
word_pattern = re.compile(r"[A-Za-z']+")

emotion_terms = {
    "joy": {"glad", "happy", "laugh", "wonderful", "great", "love", "delighted", "smile"},
    "sadness": {"sad", "sorry", "lost", "alone", "grief", "cry", "miss", "regret"},
    "anger": {"angry", "hate", "damn", "fool", "idiot", "revenge", "furious", "stop"},
    "fear": {"afraid", "fear", "scared", "danger", "run", "help", "terrified", "monster"},
    "surprise": {"what", "really", "impossible", "suddenly", "unexpected", "wait"},
    "contemplation": {"perhaps", "maybe", "wonder", "think", "remember", "understand", "why"},
}


def annotate_delivery(
    text, *, speaker="Narrator", previous_text=None, next_text=None, kind="dialogue"
):
    lowered = text.casefold()
    words = set(word_pattern.findall(lowered))
    scores = {emotion: len(words & terms) for emotion, terms in emotion_terms.items()}
    cues = []
    if "!" in text:
        scores["surprise"] += 1
        cues.append("exclamation")
    if "..." in text or "…" in text:
        scores["contemplation"] += 1
        cues.append("ellipsis")
    if text.count("?") >= 2:
        scores["surprise"] += 1
        cues.append("repeated_question")
    letters = [character for character in text if character.isalpha()]
    if len(letters) >= 8 and sum(character.isupper() for character in letters) / len(letters) > 0.7:
        scores["anger"] += 2
        cues.append("uppercase_emphasis")
    if any(token in lowered for token in ("*sob", "*cry", "tears")):
        scores["sadness"] += 2
        cues.append("sad_stage_direction")
    if any(token in lowered for token in ("*laugh", "haha", "hehe")):
        scores["joy"] += 2
        cues.append("laugh_stage_direction")

    primary, score = max(scores.items(), key=lambda item: (item[1], item[0]))
    if score == 0:
        primary = "contemplation" if kind == "narration" else "neutral"
    confidence = min(0.95, 0.45 + (score * 0.15)) if score else 0.35
    pace = "slow" if primary in {"sadness", "contemplation"} or "ellipsis" in cues else "medium"
    if primary in {"anger", "fear", "surprise"} and "ellipsis" not in cues:
        pace = "fast"
    energy = "high" if primary in {"anger", "fear", "surprise", "joy"} else "low"
    if primary == "neutral":
        energy = "medium"
    volume = "loud" if "uppercase_emphasis" in cues or text.count("!") >= 2 else "normal"
    tone = {
        "joy": "warm and buoyant",
        "sadness": "soft and restrained",
        "anger": "tense and forceful",
        "fear": "uneasy and urgent",
        "surprise": "alert and reactive",
        "contemplation": "reflective and measured",
        "neutral": "natural and conversational",
    }[primary]
    context = " ".join(value for value in (previous_text, next_text) if value)
    context_words = set(word_pattern.findall(context.casefold()))
    context_emotions = [
        emotion for emotion, terms in emotion_terms.items() if context_words & terms
    ]
    if context_emotions:
        cues.append(f"context:{context_emotions[0]}")

    generic_prompt = (
        f"Perform as {speaker}. Emotion: {primary}. Tone: {tone}. "
        f"Pace: {pace}. Energy: {energy}. Volume: {volume}."
    )
    exaggeration = 0.7 if energy == "high" else 0.45 if energy == "medium" else 0.3
    return {
        "annotation_version": delivery_annotation_version,
        "emotion": {
            "primary": primary,
            "confidence": round(confidence, 2),
            "cues": cues,
        },
        "delivery": {
            "pace": pace,
            "energy": energy,
            "volume": volume,
            "tone": tone,
        },
        "prompt_adapters": {
            "generic": generic_prompt,
            "chatterbox": {
                "prompt": generic_prompt,
                "exaggeration": exaggeration,
                "cfg_weight": 0.45 if primary in {"anger", "fear"} else 0.5,
            },
            "cosyvoice": {"instruct": generic_prompt},
            "fish_speech": {"text_prompt": generic_prompt},
        },
    }
