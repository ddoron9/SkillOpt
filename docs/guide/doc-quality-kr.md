# 한국어 문서 품질 스킬 (doc_quality_kr)

API 키 없이 Claude CLI(Claude Max 구독)만으로 "한국어 기술 문서 작성" 스킬 문서를
훈련합니다. Confluence 페이지를 reference로 삼아, 같은 지시문을 받았을 때 비슷한
품질·스타일의 문서를 만들어내도록 skill 문서를 점진적으로 다듬어 줍니다.

훈련 / 평가 / optimizer / judge 모두 Claude CLI 한 종류만 호출하므로
별도의 API key 설정은 필요 없습니다. 단, Claude Max 한도(메시지/시간당 토큰)는
공유하므로 큰 배치를 한꺼번에 돌리지 마세요.

## 데이터 형식

각 split (`train/`, `val/`, `test/`) 디렉토리에는 `items.json` 한 개가 들어가며,
JSON 배열의 각 원소가 하나의 학습/평가 과제입니다.

## 필수 / 권장 필드

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | str | 고유 식별자. Confluence 페이지 id를 그대로 쓰면 편함. |
| `instruction` | str | 모델에게 줄 작성 지시문. "신규 입사자에게 ~~ 안내 문서를 작성해줘" 식. |
| `reference_doc` | str | 사내에서 통용되는 좋은 문서(목표 스타일). 평가자가 이걸 기준으로 채점. |
| `task_type` | str | 선택. 예: `tutorial`, `runbook`, `concept`, `release_note`. |
| `audience` | str | 선택. 청자(예: "신규 입사자", "운영 담당자"). |
| `doc_type` | str | 선택. 예: "온보딩 가이드", "장애 대응 매뉴얼". |
| `style_hints` | str | 선택. 어조/제약 등 추가 힌트. |
| `context` | str | 선택. 모델이 참고할 소스 자료(코드 스니펫, 설계 노트 등). 없으면 빈 문자열. |
| `reference_meta` | dict | 선택. 출처 추적용 메타(page_id, url, version 등). |

## 권장 데이터 규모

| split | 최소 | 권장 |
|---|---|---|
| train | 12 | 30~80 |
| val   | 4  | 8~15 |
| test  | 4  | 8~20 |

너무 적으면 epoch 1회당 통계가 흔들리고, 너무 많으면 Claude Max 한도에 빠르게 부딪힙니다.
처음에는 `train=15, val=5, test=5` 정도로 시작해 1 epoch만 돌려본 뒤 늘리는 걸 권장합니다.

## 채우는 흐름

자격증명 export 후 두 모드 중 하나로 ingest 실행합니다.

### 모드 A — 본인이 작성한 페이지 자동 수집 (권장)

```bash
export CONFLUENCE_URL="https://yourcompany.atlassian.net/wiki"
export CONFLUENCE_USER="you@yourcompany.com"
export CONFLUENCE_TOKEN="atatt..."     # https://id.atlassian.com/manage-profile/security/api-tokens

python scripts/ingest_confluence.py \
    --search-current-user-pages \
    --max-pages 16 \
    --redact \
    --out_dir data/doc_quality_kr_split \
    --split_ratio 7:1:2
```

CQL `creator = currentUser()` 로 본인이 작성한 페이지를 최근 수정순으로 가져옵니다.

### 모드 B — 페이지 ID/URL 직접 지정

`pages.json` 작성:
```json
[
  {"page_id": "1234567",
   "audience": "신규 입사자",
   "doc_type": "온보딩 가이드"},
  {"url": "https://yourcompany.atlassian.net/wiki/spaces/ENG/pages/2345/Foo"}
]
```

실행:
```bash
python scripts/ingest_confluence.py \
    --input pages.json \
    --redact \
    --out_dir data/doc_quality_kr_split \
    --split_ratio 7:1:2
```

### 비식별화

`--redact` (기본 켜짐)는 `scripts/redact.py`의 패턴 기반 치환을 적용합니다.

| 카테고리 | 원본 예시 | 치환 결과 |
|---|---|---|
| 고객사명 | 현대중공업, 삼성증권 | `고객사1`, `고객사2` (등장 순) |
| 동료 이름 | 이지훈, 김도이 | `동료1`, `동료2` |
| 내부 IP | 192.168.10.81 | `내부서버1` |
| 내부 git 조직 | crowdworks_dev | `<ORG_GIT>` |
| 사내 도메인 | crowdworksinc.atlassian.net | `<COMPANY>.atlassian.net` |
| 내부 시스템 | knowledge_compiler, kc-backend | `<INTERNAL_SYS>` |
| 티켓 ID | KCP-960, FA-903 | `TICKET-1`, `TICKET-2` |
| 이메일 / 전화 | foo@bar.com / 010-1234-5678 | `이메일1` / `전화1` |
| 아바타 URL / 내부 smartlink | (긴 URL) | 제거 / `<내부링크>` |

매핑 결과는 `data/doc_quality_kr_split/redaction_mapping.json`에 저장되어
필요하면 사람이 검토할 수 있습니다. 새 식별자가 발견되면
`scripts/redact.py`의 `CUSTOMER_NAMES`, `COWORKER_NAMES`, `INTERNAL_*`
리스트에 추가하면 됩니다.

치환 후 한 번 더 leakage 패턴(`scan_for_leakage`)으로 결과를 재검사하고,
하나라도 남아 있으면 split을 쓰지 않고 중단합니다. 그래도 강행하려면
`--allow-leakage`를 추가하세요.

### 결과 확인 + 학습

3. 결과: `data/doc_quality_kr_split/{train,val,test}/items.json` 생성.
4. 학습: `bash scripts/run_doc_quality_kr.sh`

## 수동으로 만들고 싶다면

다음 형태의 JSON을 직접 작성해 `data/doc_quality_kr_split/<split>/items.json`에 저장하면 됩니다.

```json
[
  {
    "id": "manual-001",
    "task_type": "tutorial",
    "instruction": "신규 입사자에게 배포 절차를 안내하는 온보딩 가이드를 작성해줘.",
    "audience": "신규 입사자",
    "doc_type": "온보딩 가이드",
    "style_hints": "각 단계는 동사로 시작. 명령은 코드 블록으로.",
    "context": "(필요하면 참고할 코드/설계 자료를 여기에)",
    "reference_doc": "# 배포 절차\n\n신규 입사자라면 ...\n\n## 1. 권한 받기\n...",
    "reference_meta": {"source": "manual", "url": ""}
  }
]
```

`skillopt/envs/doc_quality_kr/example_item.json`에 한 줄짜리 샘플 데이터가 있습니다.
처음에는 이 파일을 `data/doc_quality_kr_split/{train,val,test}/items.json`에
복사해 두고 ingestion이 잘 되는지 확인한 뒤, Confluence에서 받은 진짜 데이터로
교체하는 것을 권장합니다.

## 학습 한 번 돌려 보기

```bash
bash scripts/run_doc_quality_kr.sh --num_epochs 1 --batch_size 4 --workers 2
```

옵션은 모두 `python scripts/train.py --help`와 동일하게 받습니다. 처음 한 번은
작은 배치로 돌려서 한 step의 비용·시간을 체감한 뒤 epoch와 batch_size를 늘리세요.

## 평가만 돌려 보기

```bash
python scripts/eval_only.py \
    --config configs/doc_quality_kr/default.yaml \
    --skill outputs/<run_name>/best_skill.md \
    --split valid_unseen \
    --split_dir data/doc_quality_kr_split
```

## 채점 기준 (LLM-as-judge)

`skillopt/envs/doc_quality_kr/evaluator.py`의 judge가 6개 차원을 각 0~5점으로
채점하고 평균을 0~1로 정규화합니다. 기본 통과 임계값은 0.7입니다.

- `natural_korean`: 자연스러운 한국어인가
- `readability`: 목차/구조가 청자의 읽는 순서를 안내하는가
- `brevity`: 같은 말 반복 없이 분량이 적절한가
- `word_choice`: 사내에서 통용되는 단어를 쓰는가 (잘 안 쓰는 한자어/외래어 회피)
- `fidelity`: 컨텍스트의 사실관계에 충실한가
- `style_match`: reference 문서의 톤·구조와 일치하는가

차원이나 임계값을 조정하려면 evaluator.py의 `_JUDGE_SYSTEM`과 config의
`env.pass_threshold`를 함께 바꿉니다.
