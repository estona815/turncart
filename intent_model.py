"""Original local softmax text classifier, with explicit abstention.

The learned weights come only from the train partition. Scores are
uncalibrated model posteriors, not probabilities of a safe payment. Every
decision requires a separate user-reviewed catalog cart before network calls.
Feature design and regularization were selected using small synthetic
development sets; these sets are not independent accuracy measurements.
"""
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata


LABELS = ("checkout", "edit", "abandon", "status")
# Grammatical/politeness words cannot establish a transactional intent.
FUNCTION_WORDS = frozenset("i me my we our you your the a an to for with these "
    "this those in on of am is are be please let can would like want need do "
    "now first".split())


class IntentError(ValueError):
    pass


def features(text, training_words=None):
    if type(text) is not str or len(text) > 2000:
        raise IntentError("text_bounds")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise IntentError("text_encoding") from None
    normalized = unicodedata.normalize("NFKC", text).lower()
    if any(c.isalpha() and not ('a' <= c <= 'z') for c in normalized):
        return Counter({"unsupported_language": 1})
    normalized = re.sub(r"\b([a-z]+)n['’]t\b", r"\1 not", normalized)
    normalized = re.sub(r"\bcheck(?:ing)?\s+out\b", "checkout", normalized)
    words = re.findall(r"[a-z]+|[,;.!?]", normalized)
    if len(words) > 256:
        raise IntentError("word_budget")
    if training_words is not None:
        # Resolve only previously unseen plural forms to a unique word already
        # present in training. No label-specific keyword, new vocabulary or
        # holdout fitting; ambiguous candidates stay unknown and may abstain.
        for i, word in enumerate(words):
            if word in training_words:
                continue
            candidates = set()
            if word.endswith("s"):
                candidates.add(word[:-1])
            if word.endswith("es"):
                candidates.add(word[:-2])
            if word.endswith("ies"):
                candidates.add(word[:-3] + "y")
            matches = candidates & training_words
            if len(matches) == 1:
                words[i] = matches.pop()
    # Negation is lexical scope, not an unconditional abandon label. Separate
    # clauses also prevent a negative instruction contaminating a later clause.
    scoped, pairs, previous, negative = [], [], None, False
    for word in words:
        if word in (",", ";", ".", "!", "?", "but", "however"):
            previous, negative = None, False
            continue
        if word in ("not", "never", "without", "no"):
            previous, negative = None, True
            continue
        if word in FUNCTION_WORDS:
            continue
        token = ("neg:" if negative else "w:") + word
        scoped.append(token)
        if previous is not None:
            pairs.append("b:" + previous + " " + token)
        previous = token
    return Counter(scoped + pairs)


@dataclass(frozen=True)
class IntentResult:
    suggested_intent: str
    posterior_scores: tuple
    known_feature_fraction: float
    reason: str
    payment_authorized: bool = False


class IntentModel:
    def __init__(self, rows):
        if type(rows) is not dict or set(rows) != set(LABELS):
            raise IntentError("training_labels")
        counts, sizes, seen, examples = {}, {}, set(), []
        for label in LABELS:
            values = rows[label]
            if type(values) is not list or not 2 <= len(values) <= 10000:
                raise IntentError("training_rows")
            count = Counter()
            for text in values:
                fs = features(text)
                if not fs or text in seen:
                    raise IntentError("training_empty_or_duplicate")
                seen.add(text)
                count.update(fs.keys())
                examples.append((label, frozenset(fs)))
            counts[label] = count
            sizes[label] = len(values)
        self.vocabulary = frozenset().union(*(set(x) for x in counts.values()))
        self.training_words = frozenset(k.split(":", 1)[1] for k in self.vocabulary
                                        if k.startswith(("w:", "neg:")))
        # Deterministic full-batch softmax fitting uses present features only.
        # Absent vocabulary no longer supplies a large short-utterance bias.
        self.biases = {label: 0.0 for label in LABELS}
        self.log_weights = {label: dict.fromkeys(sorted(self.vocabulary), 0.0)
                            for label in LABELS}
        for _ in range(2400):
            gradients = {label: Counter() for label in LABELS}
            bias_gradient = Counter()
            for actual, present in examples:
                scores = self._scores(present)
                for label in LABELS:
                    error = scores[label] - (label == actual)
                    bias_gradient[label] += error
                    for word in sorted(present):
                        gradients[label][word] += error
            for label in LABELS:
                self.biases[label] -= 0.5 * bias_gradient[label] / len(examples)
                for word in self.log_weights[label]:
                    weight = self.log_weights[label][word]
                    self.log_weights[label][word] -= 0.5 * (
                        gradients[label][word] / len(examples) + 0.0005 * weight)
        self.training_examples = sum(sizes.values())
        self.training_sha256 = hashlib.sha256(json.dumps(
            rows, sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()

    @classmethod
    def from_corpus(cls, path=None):
        path = Path(path) if path is not None else Path(__file__).with_name("corpus.json")
        with path.open("rb") as stream:
            raw = stream.read(1000001)
        if len(raw) > 1000000:
            raise IntentError("corpus_size")
        try:
            corpus = json.loads(raw)
        except (ValueError, UnicodeError):
            raise IntentError("corpus_json") from None
        # Learned weights and vocabulary use train only. Development examples
        # have informed feature design and regularization, never training rows.
        return cls(corpus["train"])

    def _scores(self, known):
        logits = {label: self.biases[label] + sum(
            self.log_weights[label][k] for k in sorted(known)) for label in LABELS}
        maximum = max(logits.values())
        weights = {k: math.exp(x - maximum) for k, x in logits.items()}
        scores = {k: x / sum(weights.values()) for k, x in weights.items()}
        return scores

    def predict(self, text):
        fs = features(text, self.training_words)
        known = {k: n for k, n in fs.items() if k in self.vocabulary}
        # Coverage measures lexical familiarity, independently of unseen word
        # pairs or scope combinations. Otherwise a familiar negative sentence
        # can be rejected solely because its new bigrams multiply the count.
        # Scope remains part of model scoring and the inversion gate below.
        lexical = {k: n for k, n in fs.items() if k.startswith(("w:", "neg:"))}
        coverage = sum(n for k, n in lexical.items()
                       if k.split(":", 1)[1] in self.training_words) / sum(
                       lexical.values()) if lexical else 0.0
        scores = self._scores(known)
        ranking = sorted(LABELS, key=lambda x: (-scores[x], x))
        best, second = ranking[:2]
        reason = "supported_intent"
        normalized = unicodedata.normalize("NFKC", text).lower()
        # These are abstention gates, never rules choosing a payment intent.
        # Quoted, conditional and explanatory language need a fresh instruction.
        # An opening quote is enough: unfinished reported speech still needs
        # a fresh instruction. Apostrophes inside unquoted contractions or
        # possessives follow a word and do not match this opening boundary.
        quoted = re.search(r"[\"“”]|(?<!\w)['‘]", normalized)
        if (quoted or re.search(r"\b(hypothetically|example|demo|explain)\b", normalized)
                or (re.search(r"\bif\b", normalized) and best != "status")):
            reason = "non_executable_or_conditional_request"
        elif best == "checkout" and re.search(
                r"\b(later|tomorrow|wait|until|once|after|eventually|someday|"
                r"maybe|perhaps|clarify|describe|estimate|compare)\b|"
                r"\bnot\s+yet\b|\bbefore\s+(?:i\s+)?decid(?:e|ing)\b",
                normalized):
            # A checkout score cannot settle timing, prerequisite actions or
            # a request for information. This gate only abstains; it never
            # chooses another intent or changes learned scores/thresholds.
            # Conservative: it may also defer a valid immediate instruction
            # whose unrelated delivery clause contains a future time word.
            reason = "deferred_or_information_request"
        elif (best == "checkout"
              and re.search(r"\b(already|existing|previous|last|yesterday|earlier|"
                            r"past|paid|purchased|ordered|completed|made)\b", normalized)
              and re.search(r"\b(order|purchase|payment|transaction|checkout|receipt|"
                            r"capture)\b", normalized)
              and re.search(r"\?|^\s*(do|did|does|has|have|is|was|were|what|where|"
                            r"when|why|how|can|could)\b|\b(show|tell|check|find|"
                            r"status|receipt|confirmation|order\s+number)\b", normalized)):
            # Historical-order questions must not become a new purchase just
            # because the model recognizes payment words. Only abstain: the
            # model has not established that a status lookup is intended.
            reason = "historical_order_query"
        elif best == "checkout" and re.search(
                r"\b(remove|replace|swap|change|edit|update|delete|adjust|add)\b|"
                r"\b(only|except|excluding)\b|"
                r"\b(yesterday|earlier|previously|formerly|ago)\b|"
                r"\b(?:was|were|had)\b.{0,30}\b(?:ready|wanted|planning|going)\b|"
                r"\bshould\s+(?:i|we)\b|"
                r"\b(?:do|would)\s+you\s+(?:think|recommend|suggest)\b",
                normalized):
            # Natural-language cart edits and past/advisory statements do not
            # establish a present instruction for the separately reviewed cart.
            # This may defer benign clauses; it never chooses another intent,
            # edits the cart, or grants payment authorization.
            reason = "cart_change_or_noncurrent_request"
        elif any(k.startswith("neg:") and k not in self.vocabulary and
                 "w:" + k[4:] in self.vocabulary and
                 max(LABELS, key=lambda label: self.log_weights[label]["w:" + k[4:]])
                 == "abandon" for k in fs):
            reason = "unfamiliar_negation_scope"
        elif coverage < 0.35:
            reason = "unfamiliar_language_or_request"
        elif scores[best] < 0.65 or scores[best] - scores[second] < 0.25:
            reason = "ambiguous_model_scores"
        result = best if reason == "supported_intent" else "clarify"
        return IntentResult(result, tuple((k, scores[k]) for k in LABELS), coverage, reason)


def development_evaluation(model):
    corpus = json.loads(Path(__file__).with_name("corpus.json").read_text())
    training = {text for texts in corpus["train"].values() for text in texts}
    holdout = corpus["development_holdout"]
    assert all(text not in training for label, text in holdout)
    confusion = {label: {k: 0 for k in (*LABELS, "clarify")} for label in LABELS}
    for label, text in holdout:
        confusion[label][model.predict(text).suggested_intent] += 1
    correct = sum(confusion[k][k] for k in LABELS)
    return {"partition": "original_small_development_holdout", "examples": len(holdout),
            "correct_including_abstentions_as_errors": correct, "confusion": confusion,
            "production_accuracy_verified": False, "training_sha256": model.training_sha256,
            "unsupported_probes": [{"text": text,
                "suggested_intent": model.predict(text).suggested_intent}
                for text in corpus["unsupported_probe"]]}


if __name__ == "__main__":
    print(json.dumps(development_evaluation(IntentModel.from_corpus()), indent=2))
