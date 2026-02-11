"""
translator.py — Azure OpenAI GPT 기반 번역 엔진

슬라이드 맥락 파악 + Run 수준 스타일 보존 번역을 수행합니다.
"""

import json
import os
import time
import logging

from openai import AzureOpenAI

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  언어 코드 → 이름 매핑
# ──────────────────────────────────────────────

LANG_MAP = {
    "ko": "한국어 (Korean)",
    "ja": "日本語 (Japanese)",
    "zh": "中文 (Chinese)",
    "en": "English",
    "es": "Español (Spanish)",
    "fr": "Français (French)",
    "de": "Deutsch (German)",
    "pt": "Português (Portuguese)",
    "it": "Italiano (Italian)",
    "vi": "Tiếng Việt (Vietnamese)",
    "th": "ภาษาไทย (Thai)",
    "id": "Bahasa Indonesia (Indonesian)",
    "ru": "Русский (Russian)",
    "ar": "العربية (Arabic)",
}


def get_lang_name(code: str) -> str:
    """언어 코드를 사람 읽기 가능한 이름으로 변환합니다."""
    return LANG_MAP.get(code, code)


# ──────────────────────────────────────────────
#  Azure OpenAI 클라이언트 (싱글톤)
# ──────────────────────────────────────────────

_client: AzureOpenAI | None = None


def _get_client() -> AzureOpenAI:
    """AzureOpenAI 클라이언트를 싱글톤으로 반환합니다."""
    global _client
    if _client is None:
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        api_key = os.getenv("AZURE_OPENAI_API_KEY")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

        if not endpoint or not api_key:
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT와 AZURE_OPENAI_API_KEY 환경변수를 설정하세요. "
                ".env.example을 참고하여 .env 파일을 생성하세요."
            )

        _client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )
    return _client


def _get_deployment() -> str:
    """배포 이름을 반환합니다."""
    return os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-52")


# ──────────────────────────────────────────────
#  API 호출 (재시도 포함)
# ──────────────────────────────────────────────

MAX_RETRIES = 3


def _call_chat(messages: list[dict], response_format: dict | None = None,
               temperature: float = 0.3) -> str:
    """
    Azure OpenAI Chat Completions API를 호출합니다.
    429 에러 시 retry-after 기반 재시도를 수행합니다.
    GPT-5.2 모델의 추론(reasoning)을 비활성화합니다.
    """
    client = _get_client()
    deployment = _get_deployment()

    for attempt in range(MAX_RETRIES):
        try:
            kwargs = {
                "model": deployment,
                "messages": messages,
                "temperature": temperature,
                "reasoning_effort": "none",
            }
            if response_format:
                kwargs["response_format"] = response_format

            response = client.chat.completions.create(**kwargs)
            return response.choices[0].message.content

        except Exception as e:
            error_str = str(e)
            # Rate limit 처리
            if "429" in error_str or "rate" in error_str.lower():
                retry_after = 10 * (attempt + 1)  # 점진적 대기
                # retry-after 헤더 파싱 시도
                try:
                    if hasattr(e, "response") and e.response is not None:
                        ra = e.response.headers.get("retry-after")
                        if ra:
                            retry_after = int(ra)
                except (AttributeError, ValueError):
                    pass
                logger.warning(
                    f"Rate limit 도달. {retry_after}초 후 재시도... "
                    f"(시도 {attempt + 1}/{MAX_RETRIES})"
                )
                time.sleep(retry_after)
                continue

            if attempt == MAX_RETRIES - 1:
                logger.error(f"API 호출 실패 (최대 재시도 초과): {e}")
                raise
            logger.warning(f"API 호출 오류, 재시도 중... ({attempt + 1}/{MAX_RETRIES}): {e}")
            time.sleep(2 * (attempt + 1))

    raise RuntimeError("최대 재시도 횟수 초과")


# ──────────────────────────────────────────────
#  Phase 0: 프레젠테이션 전체 맥락 파악 (상위 N장)
# ──────────────────────────────────────────────

def get_presentation_summary(slides_text: str, target_lang: str) -> str:
    """
    프레젠테이션 상위 슬라이드 텍스트를 분석하여 전체 목적·방향을 요약합니다.
    이 요약은 이후 모든 슬라이드 번역의 기본 맥락으로 사용됩니다.
    """
    lang_name = get_lang_name(target_lang)

    messages = [
        {
            "role": "system",
            "content": (
                "당신은 프레젠테이션 분석 전문가입니다. "
                "아래는 프레젠테이션 앞부분 슬라이드들의 텍스트입니다. "
                "이 프레젠테이션의 전체 목적, 대상 청중, 핵심 주제를 3~5문장으로 요약하세요. "
                f"이 요약은 이후 전체 슬라이드를 {lang_name}로 번역할 때 "
                "일관된 톤과 용어 선택을 위한 기본 맥락으로 사용됩니다. "
                "고유명사, 솔루션 이름, 기술 용어는 원문 그대로 언급하세요."
            ),
        },
        {
            "role": "user",
            "content": f"프레젠테이션 앞부분 텍스트:\n\n{slides_text}",
        },
    ]

    try:
        return _call_chat(messages, temperature=0.2)
    except Exception as e:
        logger.warning(f"프레젠테이션 전체 맥락 파악 실패: {e}")
        return ""


# ──────────────────────────────────────────────
#  Phase 1: 슬라이드 맥락 파악
# ──────────────────────────────────────────────

def get_slide_context(slide_text: str, target_lang: str) -> str:
    """
    슬라이드 전체 텍스트를 분석하여 맥락 요약을 반환합니다.
    """
    lang_name = get_lang_name(target_lang)

    messages = [
        {
            "role": "system",
            "content": (
                "당신은 프레젠테이션 번역 전문가입니다. "
                "아래 슬라이드의 텍스트를 읽고, 이 슬라이드의 주제와 핵심 맥락을 "
                "2~3문장으로 요약하세요. "
                f"이 요약은 이후 {lang_name}로의 번역 품질을 높이는 데 사용됩니다. "
                "고유명사, 솔루션 이름, 기술 용어는 원문 그대로 언급하세요."
            ),
        },
        {
            "role": "user",
            "content": f"슬라이드 텍스트:\n\n{slide_text}",
        },
    ]

    try:
        return _call_chat(messages, temperature=0.2)
    except Exception as e:
        logger.warning(f"맥락 파악 실패, 빈 맥락으로 진행: {e}")
        return ""


# ──────────────────────────────────────────────
#  Phase 2: 스타일 보존 번역
# ──────────────────────────────────────────────

# JSON Schema for structured response (단일 텍스트박스용)
TRANSLATION_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "translation_result",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "paragraphs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "runs": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "style_id": {"type": "string"},
                                    },
                                    "required": ["text", "style_id"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["runs"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["paragraphs"],
            "additionalProperties": False,
        },
    },
}

# JSON Schema for structured response (슬라이드 일괄 번역용)
BATCH_TRANSLATION_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "batch_translation_result",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "text_boxes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "box_id": {"type": "string"},
                            "paragraphs": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "runs": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "text": {"type": "string"},
                                                    "style_id": {"type": "string"},
                                                },
                                                "required": ["text", "style_id"],
                                                "additionalProperties": False,
                                            },
                                        },
                                    },
                                    "required": ["runs"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["box_id", "paragraphs"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["text_boxes"],
            "additionalProperties": False,
        },
    },
}


def translate_styled_text(styled_data: dict, context: str, target_lang: str,
                          pres_summary: str = "") -> dict | None:
    """
    스타일 ID가 매핑된 텍스트 데이터를 번역합니다.

    Args:
        styled_data: extract_styled_paragraphs()가 반환한 구조
        context: get_slide_context()가 반환한 맥락 요약
        target_lang: 대상 언어 코드 (예: 'ko')
        pres_summary: 프레젠테이션 전체 맥락 요약 (상위 3장 기반)

    Returns:
        {"paragraphs": [{"runs": [{"text": "...", "style_id": "S0"}, ...]}]}
        실패 시 None 반환
    """
    lang_name = get_lang_name(target_lang)

    # 번역할 텍스트 없으면 스킵
    all_text = "".join(
        run["text"]
        for para in styled_data["paragraphs"]
        for run in para["runs"]
    ).strip()
    if not all_text:
        return None

    # 스타일 정보를 사람 읽기 가능하게 변환
    styles_desc = {}
    for sid, sdict in styled_data["styles"].items():
        desc_parts = []
        if sdict.get("bold"):
            desc_parts.append("볼드")
        if sdict.get("italic"):
            desc_parts.append("이탤릭")
        if sdict.get("underline"):
            desc_parts.append("밑줄")
        if "size" in sdict:
            pt_size = sdict["size"] / 12700 if isinstance(sdict["size"], int) else sdict["size"]
            desc_parts.append(f"크기:{pt_size:.0f}pt")
        if "name" in sdict:
            desc_parts.append(f"폰트:{sdict['name']}")
        if "color_rgb" in sdict:
            desc_parts.append(f"색상:#{sdict['color_rgb']}")
        styles_desc[sid] = ", ".join(desc_parts) if desc_parts else "기본"

    # --- 입력 텍스트 구조를 보기 좋게 정리 ---
    input_paras = []
    for para in styled_data["paragraphs"]:
        input_runs = []
        for run in para["runs"]:
            input_runs.append({"text": run["text"], "style_id": run["style_id"]})
        input_paras.append({"runs": input_runs})

    input_json = json.dumps(
        {"paragraphs": input_paras, "styles_description": styles_desc},
        ensure_ascii=False,
        indent=2,
    )

    # 프레젠테이션 전체 맥락 섹션
    pres_context_section = ""
    if pres_summary:
        pres_context_section = f"""\n## 프레젠테이션 전체 맥락
이 프레젠테이션의 전체 방향과 목적입니다. 번역 시 이 맥락을 반영하여 일관된 톤과 용어를 사용하세요.
{pres_summary}
"""

    system_prompt = f"""당신은 프레젠테이션 번역 전문가입니다. 아래 규칙을 엄격히 따르세요.
{pres_context_section}
## 번역 규칙
1. 주어진 텍스트를 **{lang_name}**로 자연스럽게 번역합니다.
2. **고유명사, 솔루션 이름, 기술 용어** (예: Azure, Cosmos DB, API, SDK, GPT, Microsoft, AWS, Google 등)는 번역하지 않고 영문 원문 그대로 유지합니다.
3. 약어 및 브랜드명(예: AI, ML, IoT, SaaS 등)도 원문 그대로 유지합니다.
4. 번역 결과가 원문과 동일하더라도(예: 이미 대상 언어이거나 고유명사만인 경우) 반드시 결과를 반환하세요. 빈 텍스트로 반환하지 마세요.

## 프레젠테이션 어미 규칙
5. 프레젠테이션용 번역이므로, 구어체("-합니다", "-입니다")가 아닌 **간결한 명사형/체언형 어미**를 사용하세요.
   - 예: "데이터를 분석합니다" → "데이터 분석", "성능을 향상시킵니다" → "성능 향상"
   - 제목/키워드: 명사형 종결 (예: "실시간 데이터 처리", "글로벌 확장 지원")
   - 설명문: 간결한 문장형 (예: "~를 통해 ~를 실현", "~로 ~를 지원")
   - 단, 문맥상 완전한 문장이 자연스러운 경우(긴 설명, 인용 등)에는 "-합니다" 체를 허용합니다.

## 스타일 보존 규칙
6. 각 Run에는 `style_id`가 있으며, 이는 시각적 서식(볼드, 이탤릭, 색상 등)을 나타냅니다.
7. 번역 결과에서도 각 텍스트 조각에 적절한 `style_id`를 매핑하세요.
8. 원문에서 특정 스타일이 적용된 키워드가 있다면, 번역된 텍스트에서도 해당 키워드/의미에 동일한 `style_id`를 적용하세요.
9. 번역으로 인해 Run의 수나 경계가 달라질 수 있습니다. 그 경우:
   - 해당 텍스트의 원래 의미에 가장 가까운 스타일을 배정하세요.
   - 존재하는 `style_id` 값만 사용하세요 (새 ID를 만들지 마세요).

## 구조 규칙
10. 원문의 paragraph 수를 유지하세요 (빈 paragraph 포함).
11. **원문과 동일한 수의 Run을 유지하세요.** 서식 보존을 위해 원문의 Run 경계에 맞추어 번역 텍스트를 배분하세요.
12. 각 paragraph 내에서 의미 단위로 Run을 분할하고 스타일을 매핑하세요.
13. 빈 텍스트("")만 있는 Run은 그대로 유지하세요.

## 스타일 참조
{json.dumps(styles_desc, ensure_ascii=False, indent=2)}"""

    user_msg = f"아래 텍스트를 {lang_name}로 번역하세요:\n\n{input_json}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    try:
        result_str = _call_chat(messages, response_format=TRANSLATION_RESPONSE_SCHEMA)
        result = json.loads(result_str)

        # 기본 구조 유효성 검사
        if "paragraphs" not in result:
            logger.error("번역 결과에 'paragraphs' 키가 없습니다.")
            return None

        for para in result["paragraphs"]:
            if "runs" not in para:
                para["runs"] = []
            for run in para["runs"]:
                if "text" not in run:
                    run["text"] = ""
                if "style_id" not in run:
                    run["style_id"] = "S0"

        # ── 원문 / 번역 비교 로그 ──
        orig_text = " | ".join(
            "".join(r["text"] for r in p["runs"])
            for p in styled_data["paragraphs"]
        ).strip()
        trans_text = " | ".join(
            "".join(r["text"] for r in p["runs"])
            for p in result["paragraphs"]
        ).strip()
        logger.info(f"  [원문] {orig_text}")
        logger.info(f"  [번역] {trans_text}")

        return result

    except json.JSONDecodeError as e:
        logger.error(f"번역 결과 JSON 파싱 실패: {e}")
        return None
    except Exception as e:
        logger.error(f"번역 API 호출 실패: {e}")
        return None


# ──────────────────────────────────────────────
#  Phase 2-B: 슬라이드 일괄 번역 (텍스트박스 N개 → API 1회)
# ──────────────────────────────────────────────

def translate_slide_batch(
    text_boxes: list[dict],
    context: str,
    target_lang: str,
    pres_summary: str = "",
    recent_translations: list[dict] | None = None,
) -> dict | None:
    """
    슬라이드 내 여러 텍스트박스를 한 번의 API 호출로 일괄 번역합니다.

    Args:
        text_boxes: [{"box_id": "T0", "styled_data": {...}}, ...]
            각 항목은 extract_styled_paragraphs() 결과 + box_id
        context: 슬라이드 맥락 요약
        target_lang: 대상 언어 코드
        pres_summary: 프레젠테이션 전체 맥락 요약

    Returns:
        {"T0": {"paragraphs": [...]}, "T1": {"paragraphs": [...]}, ...}
        실패 시 None
    """
    lang_name = get_lang_name(target_lang)
    if not text_boxes:
        return {}

    # ── 전체 스타일 통합 ──
    all_styles_desc: dict[str, str] = {}
    for tb in text_boxes:
        sd = tb["styled_data"]
        for sid, sdict in sd["styles"].items():
            if sid in all_styles_desc:
                continue
            desc_parts = []
            if sdict.get("bold"):
                desc_parts.append("볼드")
            if sdict.get("italic"):
                desc_parts.append("이탤릭")
            if sdict.get("underline"):
                desc_parts.append("밑줄")
            if "size" in sdict:
                pt_size = sdict["size"] / 12700 if isinstance(sdict["size"], int) else sdict["size"]
                desc_parts.append(f"크기:{pt_size:.0f}pt")
            if "name" in sdict:
                desc_parts.append(f"폰트:{sdict['name']}")
            if "color_rgb" in sdict:
                desc_parts.append(f"색상:#{sdict['color_rgb']}")
            all_styles_desc[sid] = ", ".join(desc_parts) if desc_parts else "기본"

    # ── 입력 JSON 구성 ──
    input_boxes = []
    for tb in text_boxes:
        sd = tb["styled_data"]
        paras = []
        for para in sd["paragraphs"]:
            runs = [{"text": r["text"], "style_id": r["style_id"]} for r in para["runs"]]
            paras.append({"runs": runs})
        input_boxes.append({"box_id": tb["box_id"], "paragraphs": paras})

    input_json = json.dumps(
        {"text_boxes": input_boxes, "styles_description": all_styles_desc},
        ensure_ascii=False,
        indent=2,
    )

    # ── 프롬프트 구성 ──
    pres_context_section = ""
    if pres_summary:
        pres_context_section = f"""\n## 프레젠테이션 전체 맥락
이 프레젠테이션의 전체 방향과 목적입니다. 번역 시 이 맥락을 반영하여 일관된 톤과 용어를 사용하세요.
{pres_summary}
"""

    # ── 직전 슬라이드 번역 이력 (용어 일관성) ──
    recent_section = ""
    if recent_translations:
        # 토큰 절약: 최대 30쌍만 전달
        pairs = recent_translations[-30:]
        lines = [f"- \"{p['src']}\" → \"{p['tgt']}\"" for p in pairs]
        recent_section = "\n## 직전 슬라이드 번역 이력\n" \
            "아래는 이전 슬라이드에서 번역된 원문→번역 쌍입니다. " \
            "**동일하거나 유사한 용어가 등장하면 반드시 같은 번역을 사용하여 일관성을 유지하세요.**\n" \
            + "\n".join(lines) + "\n"

    system_prompt = f"""당신은 프레젠테이션 번역 전문가입니다. 아래 규칙을 엄격히 따르세요.
{pres_context_section}{recent_section}
## 입력 구조
- 하나의 슬라이드에 포함된 여러 텍스트 박스가 `text_boxes` 배열로 제공됩니다.
- 각 텍스트 박스는 `box_id`로 식별되며, `paragraphs` 배열을 포함합니다.
- 텍스트 박스 간 맥락을 참고하면 더 자연스러운 번역이 가능합니다.

## 번역 규칙
1. 모든 텍스트 박스를 **{lang_name}**로 자연스럽게 번역합니다.
2. **고유명사, 솔루션 이름, 기술 용어** (예: Azure, Cosmos DB, API, SDK, GPT, Microsoft, AWS, Google 등)는 번역하지 않고 영문 원문 그대로 유지합니다.
3. 약어 및 브랜드명(예: AI, ML, IoT, SaaS 등)도 원문 그대로 유지합니다.
4. 번역 결과가 원문과 동일하더라도(예: 이미 대상 언어이거나 고유명사만인 경우) 반드시 결과를 반환하세요. 빈 텍스트로 반환하지 마세요.

## 프레젠테이션 어미 규칙
5. 프레젠테이션용 번역이므로, 구어체("-합니다", "-입니다")가 아닌 **간결한 명사형/체언형 어미**를 사용하세요.
   - 예: "데이터를 분석합니다" → "데이터 분석", "성능을 향상시킵니다" → "성능 향상"
   - 제목/키워드: 명사형 종결 (예: "실시간 데이터 처리", "글로벌 확장 지원")
   - 설명문: 간결한 문장형 (예: "~를 통해 ~를 실현", "~로 ~를 지원")
   - 단, 문맥상 완전한 문장이 자연스러운 경우(긴 설명, 인용 등)에는 "-합니다" 체를 허용합니다.

## 스타일 보존 규칙
6. 각 Run에는 `style_id`가 있으며, 이는 시각적 서식(볼드, 이탤릭, 색상 등)을 나타냅니다.
7. 번역 결과에서도 각 텍스트 조각에 적절한 `style_id`를 매핑하세요.
8. 원문에서 특정 스타일이 적용된 키워드가 있다면, 번역된 텍스트에서도 해당 키워드/의미에 동일한 `style_id`를 적용하세요.
9. 번역으로 인해 Run의 수나 경계가 달라질 수 있습니다. 그 경우:
   - 해당 텍스트의 원래 의미에 가장 가까운 스타일을 배정하세요.
   - 존재하는 `style_id` 값만 사용하세요 (새 ID를 만들지 마세요).

## 구조 규칙
10. 각 텍스트 박스의 `box_id`를 결과에서 그대로 반환하세요. 순서도 유지하세요.
11. 각 텍스트 박스 내 paragraph 수를 유지하세요 (빈 paragraph 포함).
12. **원문과 동일한 수의 Run을 유지하세요.** 서식 보존을 위해 원문의 Run 경계에 맞추어 번역 텍스트를 배분하세요.
13. 빈 텍스트("")만 있는 Run은 그대로 유지하세요.

## 스타일 참조
{json.dumps(all_styles_desc, ensure_ascii=False, indent=2)}"""

    user_msg = f"아래 슬라이드의 텍스트 박스들을 {lang_name}로 번역하세요:\\n\\n{input_json}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    try:
        result_str = _call_chat(messages, response_format=BATCH_TRANSLATION_RESPONSE_SCHEMA)
        result = json.loads(result_str)

        if "text_boxes" not in result:
            logger.error("일괄 번역 결과에 'text_boxes' 키가 없습니다.")
            return None

        # box_id → 번역 결과 매핑
        translated_map: dict[str, dict] = {}
        for tb in result["text_boxes"]:
            box_id = tb.get("box_id", "")
            para_data = {"paragraphs": tb.get("paragraphs", [])}

            # 구조 보정
            for para in para_data["paragraphs"]:
                if "runs" not in para:
                    para["runs"] = []
                for run in para["runs"]:
                    if "text" not in run:
                        run["text"] = ""
                    if "style_id" not in run:
                        run["style_id"] = "S0"

            translated_map[box_id] = para_data

        # ── 로그 ──
        for tb in text_boxes:
            box_id = tb["box_id"]
            orig_text = " | ".join(
                "".join(r["text"] for r in p["runs"])
                for p in tb["styled_data"]["paragraphs"]
            ).strip()
            if box_id in translated_map:
                trans_text = " | ".join(
                    "".join(r["text"] for r in p["runs"])
                    for p in translated_map[box_id]["paragraphs"]
                ).strip()
                logger.info(f"  [{box_id}] \"{orig_text}\" → \"{trans_text}\"")
            else:
                logger.warning(f"  [{box_id}] 번역 결과 누락 — 원문 유지")

        return translated_map

    except json.JSONDecodeError as e:
        logger.error(f"일괄 번역 결과 JSON 파싱 실패: {e}")
        return None
    except Exception as e:
        logger.error(f"일괄 번역 API 호출 실패: {e}")
        return None


# ──────────────────────────────────────────────
#  편의: 단순 텍스트 번역 (표 셀 등)
# ──────────────────────────────────────────────

def translate_simple_text(text: str, context: str, target_lang: str) -> str | None:
    """
    단순 텍스트를 번역합니다 (스타일 매핑 불필요한 경우).
    실패 시 None을 반환합니다.
    """
    if not text.strip():
        return text

    lang_name = get_lang_name(target_lang)
    messages = [
        {
            "role": "system",
            "content": (
                f"프레젠테이션 번역 전문가입니다. 텍스트를 {lang_name}로 번역하세요. "
                "고유명사, 솔루션 이름, 기술 용어는 원문 그대로 유지합니다. "
                "번역된 텍스트만 출력하세요. 설명이나 부연을 추가하지 마세요."
                f"\n\n맥락: {context}" if context else ""
            ),
        },
        {
            "role": "user",
            "content": text,
        },
    ]

    try:
        return _call_chat(messages, temperature=0.2)
    except Exception as e:
        logger.warning(f"단순 번역 실패: {e}")
        return None
