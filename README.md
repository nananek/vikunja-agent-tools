# vikunja-agent-tools

Vikunja を中央タスク管理基盤として、複数の AI エージェント (Claude Code, Open WebUI 上のモデルなど)
の作業状態を一元管理するためのツール群。

- Claude Code 向け: MCP サーバー (`vikunja_agent_tools.mcp_server`)
- Open WebUI 向け: Python Tool (`vikunja_agent_tools.openwebui_tools`)

両方とも共通のサービス層 (`task_service.py`) を通じて Vikunja REST API を操作するため、
どちらから操作しても同じ状態ラベル・同じタスク説明フォーマットで記録が残る。

## 想定構成

```text
   Claude Code              Open WebUI                 iPhone (mDone 等)
   (MCP stdio クライアント)   (Python Tool)               (Vikunja クライアントアプリ)
        │                        │                              │
        │ stdio (MCP)            │ 関数呼び出し                  │
        ▼                        ▼                              │
   ┌───────────────────────────────────────────┐                │
   │              vikunja-agent-tools           │                │
   │  mcp_server.py        openwebui_tools.py   │                │
   │          └────────┬─────────┘              │                │
   │                task_service.py             │                │
   │                    │                        │                │
   │              vikunja_client.py              │                │
   └────────────────────┬────────────────────────┘                │
                         │ REST API (Bearer token)                 │
                         ▼                                         │
                  ┌─────────────┐                                  │
                  │   Vikunja   │◀─────────────────────────────────┘
                  │ (中央タスク管理) │            REST API
                  └─────────────┘
```

エージェントはタスクの作成・内容/開始日/期限の更新・開始・進捗報告・完了/失敗をすべて Vikunja 上のタスクとして記録する。
状態は `status-*` ラベルとタスク説明欄内の meta ブロックの両方に保持されるため、Vikunja の
Web UI やモバイルアプリからも状態を確認できる。

## 必要要件

- Python 3.12 以上
- [uv](https://docs.astral.sh/uv/) (推奨。無い場合は `pip` + `venv` でも可)
- Vikunja インスタンス (API トークン発行済みであること)
- (任意) Docker / Docker Compose

## インストール手順

```bash
git clone <このリポジトリのURL> vikunja-agent-tools
cd vikunja-agent-tools

# uv を使う場合
uv sync --extra dev

# uv が無い場合
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

設定ファイルを用意する:

```bash
cp .env.example .env
chmod 600 .env   # トークンを含むため、自分だけが読めるようにする
```

`.env` を編集し、`VIKUNJA_BASE_URL` と `VIKUNJA_API_TOKEN` を設定する。`.env` は `.gitignore` 済みで、
リポジトリにコミットされることはない。

## 環境変数一覧

`.env.example` に記載されている、設定可能な環境変数は以下の7つ。

| 変数名 | 必須 | 既定値 | 説明 |
|---|---|---|---|
| `VIKUNJA_BASE_URL` | ✅ | - | Vikunja インスタンスのベース URL (`/api/v1` は含めない) |
| `VIKUNJA_API_TOKEN` | ✅ | - | Vikunja の API トークン。**絶対に公開しないこと** |
| `VIKUNJA_PROJECT_ID` | - | (なし) | タスク作成時のデフォルトプロジェクト ID |
| `VIKUNJA_TIMEOUT_SECONDS` | - | `15` | Vikunja API へのリクエストタイムアウト (秒) |
| `VIKUNJA_VERIFY_TLS` | - | `true` | TLS 証明書検証の有無 (自己署名証明書等でのみ `false`) |
| `VIKUNJA_AGENT_LABEL_PREFIX` | - | `agent-` | エージェント識別用ラベルの接頭辞 |
| `VIKUNJA_STATUS_LABEL_PREFIX` | - | `status-` | 状態表現用ラベルの接頭辞 |

このほか、ハートビートの抑制間隔 (`VIKUNJA_HEARTBEAT_INTERVAL_SECONDS`、既定 900 秒) と
stale running 判定の閾値 (`VIKUNJA_STALE_RUNNING_MINUTES`、既定 30 分) もコード側のデフォルト値を
持つ設定値として存在する。通常はデフォルトのままで問題ないため `.env.example` には含めていないが、
必要であれば同名の環境変数で上書きできる。

## Claude Code 向け MCP 設定例

### uv 版

```json
{
  "mcpServers": {
    "vikunja-agent-tools": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/absolute/path/to/vikunja-agent-tools",
        "vikunja-mcp"
      ]
    }
  }
}
```

`--directory` で指定したディレクトリの `.env` が自動的に読み込まれるため、トークンをこの JSON
自体に書く必要はない。

### docker compose 版

```json
{
  "mcpServers": {
    "vikunja-agent-tools": {
      "command": "docker",
      "args": [
        "compose",
        "--project-directory", "/absolute/path/to/vikunja-agent-tools",
        "run", "--rm", "-T",
        "vikunja-agent-tools"
      ]
    }
  }
}
```

`-T` (擬似 TTY を割り当てない) が stdio ベースの MCP 通信に必須。こちらも `.env` を
`env_file` として読み込むため、トークンを JSON に書く必要はない。

### トークンを安全に渡す方法

- トークンは必ず `.env` 経由で渡し、MCP 設定 JSON や `docker-compose.yml` に直接書かない。
- `.env` は `chmod 600` にし、`.gitignore` 済みであることを確認する (このリポジトリでは初期状態で
  gitignore 済み)。
- ログ・例外メッセージにはトークン文字列を一切出力しない設計になっている
  (`grep` で確認する方法は下記「安全性・制限事項」を参照)。

## Open WebUI 設定手順

1. Open WebUI の管理画面 → **Workspace** → **Tools** を開く。
2. 「+」ボタンから新しい Tool を作成する。
3. リポジトリ直下の `openwebui_tool.py` の内容をそのままコピーして、エディタに貼り付ける。
4. Tool 名を `vikunja-agent-tools` など分かりやすい名前にして保存する。
5. Tool の Valves に `VIKUNJA_BASE_URL`、`VIKUNJA_API_TOKEN`、必要なら
   `VIKUNJA_PROJECT_ID` を設定する。貼り付けるファイルは Open WebUI に組み込まれている
   `pydantic` 以外の依存がなく、このリポジトリの Python パッケージや Docker ネットワークには依存しない。
6. 使いたいモデル/チャットの設定画面でこの Tool を有効化する。

## AI エージェント運用ルール

エージェントに Vikunja 上のタスクを管理させる際は、以下のルールをシステムプロンプト等に含めておく
ことを推奨する (そのままコピーして利用可能)。

```text
あなたは vikunja-agent-tools 経由で Vikunja 上のタスクを管理しながら作業する AI エージェントです。
以下のルールに従ってください。

1. 作業を始める前に、担当するタスクを create_agent_task で新規作成するか、
   既存タスクを get_agent_task / list_agent_tasks で確認し、担当タスクを明確にすること。
2. 実際に作業に着手したら、必ず start_agent_task を呼んで running 状態にすること。
3. 作業中は report_agent_progress または heartbeat_agent_task で定期的に状況を報告すること。
   目安として、大きな区切り (方針決定、主要な変更の完了など) ごと、または少なくとも
   15〜30分に1回は報告すること。
4. 自分の作業ではどうにもならない外部要因 (依存タスク待ち、権限不足など) で進められない場合は
   block_agent_task を呼び、状況を記録すること。
5. 人間の判断や承認が必要な場合は request_agent_input を呼んでタスクを止め、
   人間からの応答を待つこと。無断で進めないこと。
6. 作業が完了したら、必ず complete_agent_task を呼び、result_summary に成果の要約を
   記録すること。これを呼ぶまでタスクは完了とみなさないこと。
7. エラーで続行不能になった場合は fail_agent_task を呼び、原因をメッセージに記録すること。
    自己判断でタスクを completed 扱いにしないこと。
8. 既存タスクのタイトル、説明、優先度、開始日、期限を変更する場合は update_agent_task を使い、
   同じ内容のタスクを重複作成しないこと。
```

## 状態ラベル一覧

| 状態 (`AgentStatus`) | ラベル名 (既定) | 意味 |
|---|---|---|
| `queued` | `status-queued` | 未着手 |
| `running` | `status-running` | 実行中 |
| `blocked` | `status-blocked` | 外部要因で進行が止まっている |
| `needs-input` | `status-needs-input` | 人間の判断/入力待ち |
| `completed` | `status-completed` | 完了 (Vikunja 上でも `done: true` になる) |
| `failed` | `status-failed` | 失敗 (Vikunja 上のタスクは完了にせず開いたまま) |
| `cancelled` | `status-cancelled` | キャンセル |

このほか、`agent-{agent_id}` という形式のラベル (既定接頭辞 `agent-`) がタスクの担当エージェントを
示す。

## 安全性・制限事項

- **削除系操作は実装していない**: タスク削除・プロジェクト削除などの破壊的操作は、
  `vikunja_client.py` を含むどの層にも実装していない。エージェントがタスクや
  プロジェクトを消してしまうことはできない。
- 一覧取得・詳細取得・コメント追加・状態更新・進捗報告は、追加の確認フローなしに実行できる
  (これらは非破壊的な操作のため)。
- API トークンは `pydantic.SecretStr` で保持し、ログ・例外メッセージ・MCP/Tool のエラー応答に
  トークン文字列が出力されないことをテスト (`test_vikunja_client.py`) で確認している。
  実運用でも、Claude Code から実際にツールを呼び出した際の標準エラー出力 (ログ) にトークンが
  混入していないかを念のため確認したい場合は、次のように `grep` する:

  ```bash
  # Claude Code の MCP ログファイル (または docker compose logs の出力) に対して
  grep -i "$VIKUNJA_API_TOKEN" <ログファイルのパス>   # 何も出力されなければ安全
  ```

- 4xx (401/404 など) はリトライしない。429 と 5xx、および DNS 障害等の接続エラーのみ、
  指数バックオフで最大3回まで再試行する。

## テスト実行方法

外部の Vikunja サーバーは一切不要 (`respx` による HTTP モックと、インメモリの
`FakeVikunjaClient` のみで完結する)。

```bash
uv run pytest
# もしくは
pytest
```

## Docker での起動方法

```bash
docker compose build
docker compose run --rm -T vikunja-agent-tools
```

`docker-compose.yml` は `.env` を `env_file` として読み込む1サービス構成。Docker イメージの主目的は
MCP サーバー (`vikunja-mcp`, stdio) の起動であり、Open WebUI 側の Tool (`openwebui_tool.py`) は
Open WebUI の管理画面に直接貼り付けて使う運用を想定している。

## License

[MIT](./LICENSE)
