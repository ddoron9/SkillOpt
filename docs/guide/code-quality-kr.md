# 코드 품질 스킬 (code_quality_kr)

사내 Bitbucket의 머지된 PR을 reference로 삼아, Claude Code CLI가 같은 종류의
변경 지시를 받았을 때 비슷한 품질·스타일의 diff를 만들어내도록 skill
문서를 점진적으로 다듬어 줍니다.

타겟 백엔드는 `claude_code_exec` 입니다 — Claude Code CLI가 workspace에서
직접 파일을 편집하고, 그 결과 diff를 [evaluator.py](../../skillopt/envs/code_quality_kr/evaluator.py)의
LLM-as-judge가 reference diff와 비교해 채점합니다.

## 데이터 형식

각 split (`train/`, `val/`, `test/`)에 `items.json` 한 개. 각 원소:

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | str | 고유 식별자 (예: `bitbucket-wks-repo-42`) |
| `instruction` | str | 무엇을 해야 하는지 자연어 지시 (보통 PR 제목 + 본문) |
| `style_hints` | str | 선택. 어조/제약 등 추가 힌트 |
| `context_files` | list | `[{path, content}, ...]` — 변경 전 파일 내용 |
| `reference_diff` | str | 머지된 PR의 unified diff (목표) |
| `task_type` | str | 선택. 예: `refactor`, `bugfix`, `feature` |
| `reference_meta` | dict | 출처 메타 (workspace, repo, pr_id, url) |

## Bitbucket ingest

### 자격증명

```bash
export BITBUCKET_USER="your-bitbucket-username"   # email 아님, username
export BITBUCKET_APP_PASSWORD="ATBB..."           # App password (Repositories read + Pull requests read 스코프)
# export BITBUCKET_URL="https://api.bitbucket.org"  # 기본값
```

App password 발급: Bitbucket → Personal settings → App passwords → Create app password.

### 모드 A — 본인 PR 자동 수집 (권장)

```bash
python scripts/ingest_bitbucket.py \
    --search-current-user-prs \
    --workspace yourworkspace \
    --repos repo-a,repo-b,repo-c \
    --max-prs 20 \
    --state MERGED \
    --redact \
    --out_dir data/code_quality_kr_split \
    --split_ratio 7:1:2
```

각 PR에 대해 metadata + 머지된 diff를 받아오고, diff에서 추출한 파일 경로의
변경 전 내용을 base commit에서 한 번씩 fetch해 `context_files`에 넣습니다.
한 PR당 최대 8개 파일, 파일당 8000자까지 수집합니다.

### 모드 B — PR ID 직접 지정

`prs.json` 작성:
```json
[
  {"workspace": "yourworkspace", "repo": "repo-a", "pr_id": 42},
  {"workspace": "yourworkspace", "repo": "repo-b", "pr_id": 17, "style_hints": "snake_case 통일"}
]
```

실행:
```bash
python scripts/ingest_bitbucket.py \
    --input prs.json \
    --redact \
    --out_dir data/code_quality_kr_split
```

### 비식별화

`--redact` (기본 켜짐)은 `scripts/redact.py`의 패턴을 PR 본문/diff/파일
내용에 적용합니다. 동일한 매핑 테이블이 모든 PR에 일관되게 적용됩니다.
매핑 결과는 `data/code_quality_kr_split/redaction_mapping.json` 에 저장되며,
새 식별자가 발견되면 `scripts/redact.py` 의 `CUSTOMER_NAMES`,
`COWORKER_NAMES`, `INTERNAL_*` 리스트에 추가해 재실행합니다.

## 학습 한 번 돌려 보기

```bash
bash scripts/run_code_quality_kr.sh --num_epochs 1 --batch_size 4 --workers 1
```

코드 작업은 doc 작업보다 한 step이 훨씬 무겁습니다 (Claude Code CLI 호출 +
workspace 생성 + diff 계산 + judge). 처음에는 `workers=1`, `batch_size=4`로
한 step 시간을 체감한 뒤 늘리세요.

## 평가만 돌려 보기

```bash
python scripts/eval_only.py \
    --config configs/code_quality_kr/default.yaml \
    --skill outputs/<run_name>/best_skill.md \
    --split valid_unseen \
    --split_dir data/code_quality_kr_split
```

## 채점 기준 (LLM-as-judge)

`skillopt/envs/code_quality_kr/evaluator.py`의 judge가 6개 차원을 각
0~5점으로 채점하고 평균을 0~1로 정규화합니다. 기본 통과 임계값 0.7.

- `correctness`: 지시문을 충족하고 동작이 깨지지 않는가
- `minimality`: 요구 범위만 건드렸는가 (부수 리팩토링 없음)
- `no_over_engineering`: 미래 가정·과한 추상화·헛 옵션 없음
- `readability`: 변수/함수 이름·구조가 추적 가능한가
- `style_match`: reference diff의 컨벤션을 따르는가
- `no_redundancy`: 같은 일을 두 번 하지 않는가

차원이나 임계값 조정은 evaluator.py의 `_JUDGE_SYSTEM`과 config의
`env.pass_threshold`를 함께 바꿉니다.
