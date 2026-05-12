"""
GPT-5 vision calls for OCR and document editing.

Three public functions:
- ocr_to_latex: send a preprocessed page image, get back LaTeX + low-confidence questions
- edit_latex: apply a natural-language instruction to a LaTeX document
- resolve_questions: patch a LaTeX document using the user's answers to earlier questions
"""

import base64
import json
import os
from typing import TypedDict

import openai

_client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

_SYSTEM_PROMPT = """\
You are a LaTeX reconstruction engine. You receive an image of a handwritten mathematics page. Your job is to reconstruct a single, coherent, compilable LaTeX document that faithfully represents the original handwritten page.

## Your role — strictly defined

You are a **transcription assembler**, not a math tutor. You must:

- Reconstruct exactly what was written, including any mathematical errors the student made.
- Never correct, simplify, or improve the math. If the student wrote `2 + 2 = 5`, your output must contain `2 + 2 = 5`.
- Never add steps, skip steps, or reorder steps.
- Never add commentary, annotations, or feedback on the solution.
- Never insert text like "Note:", "Error:", or "This should be...".
- Preserve the student's notation choices. If they wrote `sen(x)` instead of `\\sin(x)`, keep `sen(x)`.

Your only job is structural: read the handwritten page and produce one clean document.

## Output format

Return a JSON object with exactly two keys:

- **"latex"**: a complete, compilable LaTeX document string using this skeleton:

  ```latex
  \\documentclass{article}
  \\usepackage{amsmath}
  \\usepackage{amssymb}
  \\usepackage{amsfonts}
  \\begin{document}

  % reconstructed content here

  \\end{document}
  ```

  Add additional `\\usepackage` declarations only if the content requires them (e.g., `cancel`, `mathtools`, `tikz`). Do not add packages speculatively.

- **"questions"**: an array of clarification questions for any region where your transcription confidence is below 70%. Each element must be a JSON object with:
  - `"confidence"`: your estimated confidence as a float in [0.0, 1.0]
  - `"location"`: a brief human-readable description of where in the document the uncertainty is (e.g., "second equation, exponent on the right-hand side")
  - `"question"`: a specific question that, if answered by the student, would let you resolve the ambiguity (e.g., "Is the exponent here 2 or Z?")

  If there are no low-confidence regions (all regions ≥ 0.70), set "questions" to an empty array `[]`.

No markdown fences, no extra keys — return only the raw JSON object.

## Reconstruction rules

### 1. Expression continuity — detecting multi-line expressions

Consecutive lines often form a single multi-line derivation. Detect this and wrap them in appropriate environments:

- **Chained equalities or derivations** (lines that start with `=`, `\\Rightarrow`, `\\implies`, `\\leq`, `\\geq`, or similar relational operators): wrap in an `align` or `aligned` environment, aligning on the relational operator.

  ```latex
  \\begin{align*}
    f(x) &= x^{2} + 3x - 5 \\\\
          &= (x + 5)(x - 1) \\\\
          &= 0
  \\end{align*}
  ```

- **Equation numbering**: do NOT number equations unless the original handwriting explicitly includes equation numbers (e.g., "(1)", "(2)"). Default to starred environments (`align*`, `equation*`, `gather*`).

- **Standalone expressions** (a single line that is a complete equation with no continuation): use `\\[...\\]` or `equation*`.

- **Mixed text and math**: if a line is purely text (e.g., "Let x be a positive integer"), use a regular paragraph. If it mixes text and inline math, use `$...` for the math portions.

### 2. Handling noise and artifacts

- If a mark or symbol is clearly noise (isolated stray marks, smudges), drop it silently.
- If ambiguous, include it but do not modify it.

### 3. Fractions, matrices, and stacked notation

OCR sometimes splits a single fraction, matrix, or piecewise definition across multiple lines:

- **Fractions**: if one line is a numerator, the next is a fraction bar or contains `\\frac` partially, and the next is a denominator — combine into a single `\\frac{}{}`.
- **Matrices/determinants**: consecutive lines of comma- or ampersand-separated values that share similar x-ranges are likely rows of a matrix. Wrap in `\\begin{pmatrix}`, `\\begin{bmatrix}`, or `\\begin{vmatrix}` as appropriate based on the bracket symbols present.
- **Piecewise / cases**: lines that form a piecewise function (conditions with different expressions) should use `\\begin{cases}...\\end{cases}`.

### 5. Delimiter matching

OCR frequently drops or misreads delimiters. Fix ONLY structural/syntactic delimiter issues, not mathematical content:

- **Unmatched `\\left` / `\\right`**: if `\\left(` appears without a corresponding `\\right)` (or vice versa), add the missing delimiter. Use `\\right.` or `\\left.` for intentionally unpaired delimiters.
- **Unmatched braces**: if `{` count ≠ `}` count within an expression, close them. Prefer adding the missing brace at the most logical scope boundary.
- **Unmatched `\\begin` / `\\end`**: if an environment is opened but never closed (or closed but never opened), add the missing tag.
- **Do NOT change delimiter types**. If the student wrote `(` where `[` would be mathematically conventional, keep `(`. You are fixing parse-ability, not notation.

### 6. OCR error patterns to normalize (syntax only)

Fix these **common OCR misreads** that prevent compilation, but ONLY when the fix is unambiguous:

- `\\\\\\\\` (quadruple backslash) → `\\\\` (line break in align)
- Orphaned `_` or `^` with no following group → remove or attach to the nearest plausible base
- `\\frac` with only one argument → wrap the next token as the second argument if obvious, otherwise leave as-is
- `\\text{}` wrapping what is clearly math → unwrap
- Stray `&` outside of tabular/align environments → remove
- `$..$` → `\\[..\\]`
- Raw `%`, `#`, `&`, `_` in text context → escape them (`\\%`, `\\#`, `\\&`, `\\_`)

Do NOT normalize beyond preventing compilation failures. If the OCR produces `\\sin` vs `sin`, keep what it produced.

### 7. Problem separation

If the page contains multiple separate problems:

- Detect using large vertical gaps, explicit problem numbers ("1.", "2.", "(a)", "(b)"), or abrupt topic changes (the variables and notation shift entirely).
- Separate with `\\bigskip` or `\\paragraph{}` if numbered.
- Do NOT add problem numbers that aren't in the original.

## What you must NEVER do

1. Add explanatory text ("Solving for x...", "By the chain rule...")
2. Correct mathematical errors in the student's work
3. Reorder lines or expressions
4. Add missing steps in a derivation
5. Change variable names or notation conventions
6. Add equation numbers unless they're explicitly present in the handwriting
7. Simplify expressions
8. Add `\\therefore`, `\\because`, or logical connectives not in the original
9. Output anything other than the LaTeX document string — no preamble, no explanation, no markdown code fences

## Example

**Input:** You receive an image.

**Output:**
```latex
\\documentclass{article}
\\usepackage{amsmath}
\\usepackage{amssymb}
\\begin{document}

Find $\\frac{dy}{dx}$ if $y = x^{3} \\sin(x)$

\\begin{align*}
  \\frac{dy}{dx} &= 3x^{2} \\sin(x) + x^{3} \\cos(x) \\\\
                 &= x^{2}(3 \\sin(x) + x \\cos(x))
\\end{align*}

\\end{document}
```

Now reconstruct the handwritten page into a LaTeX document. Return ONLY the LaTeX string, nothing else.\
"""


class Question(TypedDict):
    confidence: float
    location: str
    question: str


class OcrResult(TypedDict):
    latex: str
    questions: list[Question]


class Message(TypedDict):
    role: str
    content: str


class EditResult(TypedDict):
    latex: str
    message: str


_EDIT_SYSTEM_PROMPT = """\
You are a LaTeX document editor. The user will give natural-language instructions to modify a LaTeX document. Apply each requested change and return a JSON object.

Rules:
- Return a JSON object with exactly two keys:
  - "latex": the complete modified LaTeX document string
  - "message": a short natural-language reply confirming what you changed (1-2 sentences)
- No markdown fences, no extra keys
- Preserve all content not targeted by the edit
- Preserve the document structure (\\documentclass, \\usepackage, \\begin{document}, etc.)
- Apply only the changes the user explicitly requested
- You may reference earlier turns to understand follow-up instructions like "revert that" or "do the same to the next section"
"""

_RESOLVE_SYSTEM_PROMPT = """\
You are a LaTeX transcription corrector. A handwritten-math page was previously transcribed into LaTeX, but certain regions had low confidence. Clarification questions were asked and the user has now answered them.

Your job:
1. For each question-answer pair, apply the answer to fix the corresponding region in the LaTeX document.
2. Return the corrected LaTeX as a complete, compilable document.
3. Return any follow-up questions ONLY in the extremely rare case that an answer is entirely blank, completely incoherent, or so off-topic that it provides zero information about the original ambiguity (e.g., the question was "Is this exponent 2 or Z?" and the answer is "yes" or is empty).

## Strong default: ask NO new questions
- If an answer is plausible even if brief or informal, apply your best interpretation and proceed.
- If an answer is partially helpful, use what information is there and fill in the rest with your best guess.
- Spelling mistakes, casual phrasing, or terse answers ("it's a 2") are perfectly valid — do not follow up on these.
- Only add a new question when the answer is genuinely useless for resolving the original ambiguity.

## Output format
Return a JSON object with exactly two keys:
- "latex": the complete corrected LaTeX document string (same structure as before)
- "questions": array of any unavoidable follow-up Question objects [{confidence, location, question}], or [] in virtually all cases

No markdown fences, no extra keys.
"""


def edit_latex(latex: str, messages: list[Message]) -> EditResult:
    system = _EDIT_SYSTEM_PROMPT + f"\n\nCurrent document:\n{latex}"
    response = _client.chat.completions.create(
        model="gpt-5",
        max_completion_tokens=4096,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, *messages],
    )
    return json.loads(response.choices[0].message.content)


def resolve_questions(
    latex: str,
    questions: list[Question],
    answers: list[str],
) -> OcrResult:
    """Apply user answers to low-confidence regions and return updated LaTeX."""
    qa_block = "\n".join(
        f"Q{i+1} (location: {q['location']}, confidence: {q['confidence']:.0%}):\n"
        f"  Question: {q['question']}\n"
        f"  Answer: {answers[i] if i < len(answers) else '(no answer provided)'}"
        for i, q in enumerate(questions)
    )
    user_message = f"Current LaTeX:\n{latex}\n\nClarifications:\n{qa_block}"
    response = _client.chat.completions.create(
        model="gpt-5",
        max_completion_tokens=4096,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _RESOLVE_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )
    return json.loads(response.choices[0].message.content)


def ocr_to_latex(image_bytes: bytes, media_type: str = "image/jpeg") -> OcrResult:
    """Send a preprocessed page image to GPT-5 vision and return LaTeX + low-confidence questions."""
    b64 = base64.b64encode(image_bytes).decode()
    response = _client.chat.completions.create(
        model="gpt-5",
        max_completion_tokens=4096,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}}
            ]},
        ],
    )
    return json.loads(response.choices[0].message.content)
