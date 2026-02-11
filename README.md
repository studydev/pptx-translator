# PowerPoint 번역기

> Azure OpenAI GPT로 PPTX를 **서식 그대로** 다국어 번역하는 Python 기반 CLI 도구

## 번역 지원 기능

| 특징 | 설명 |
|------|------|
| ⚡ **슬라이드 일괄 번역** | 슬라이드당 1회 API 호출로 빠르고 자연스러운 번역 |
| 📝 **서식 보존** | 색상·그라데이션·폰트·크기 등 원본 서식 유지 |
| 🧠 **맥락 인식** | 상위 5장 분석 → 일관된 톤·용어 유지 |
| 🔤 **다국어 폰트 자동** | CJK·아랍어·태국어·키릴 등 스크립트별 최적 폰트 자동 설정 |
| 🌍 **14개 언어** | ko, ja, zh, en, es, fr, de, pt, it, vi, th, id, ru, ar |

---

## 🚀 시작하기

### 1. 설치

```bash
pip install -r requirements.txt
```

### 2. 환경 설정

```bash
cp .env.example .env
```

`.env`에 Azure OpenAI 자격 증명을 입력:

```dotenv
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-api-key
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-52
AZURE_OPENAI_API_VERSION=2025-04-01-preview
```

### 3. 실행

```bash
python main.py work.pptx ko                    # 전체 → 한국어
python main.py work.pptx ja -o output.pptx      # 일본어, 출력 지정
python main.py work.pptx ko -s 3-10             # 3~10번 슬라이드만
python main.py work.pptx ko -v                  # 상세 로그
```

<details>
<summary>📋 전체 옵션</summary>

```
python main.py [-h] [-o OUTPUT] [-s SLIDES] [-v] input_file target_lang

input_file       번역할 PPTX 파일
target_lang      대상 언어 코드 (ko, ja, zh, en, ...)
-o, --output     출력 파일 경로 (기본: 원본명_언어코드.pptx)
-s, --slides     슬라이드 범위 (예: 5, 3-10)
-v, --verbose    상세 로그 출력
```

</details>

---

## 🔄 번역 파이프라인

```
  PPTX Load ─── python-pptx parsing
       │
       ▼
  Phase 0 ───── presentation context   (top 5 slides, 1 API call)
       │
       ▼
  ┌─► Batch ──── translate all items    (1 API call / slide)
  │    │         text boxes + table cells
  │    ▼
  │   Apply ──── XML <a:t> replace      (style preserved)
  │    │
  └────┘  next slide
       │
       ▼
  Save ───────── output_ko.pptx
```

---

## 📁 구조

```
transppt/
  ├── main.py ··········· CLI + pipeline orchestration
  ├── pptx_handler.py ··· PPTX parse / XML style engine
  ├── translator.py ····· Azure OpenAI translation API
  ├── requirements.txt
  ├── .env.example
  └── LICENSE
```

---

## 📄 라이선스

[MIT](LICENSE)
