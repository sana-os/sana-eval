# sana-eval

SANA OS（https://github.com/sana-os/sana-os ）の文面を変えたとき、その変更だけでモデルの応答がどう変わるかを、同じ条件で比べるための最小の試験装置です。

SANA OS の [decision 0003](https://github.com/sana-os/sana-os/blob/main/decisions/0003-repository-position.md) で決めたとおり、SANA OS のリポジトリは正本（原文）で、特定のプラットフォームでは動きません。AI に渡す文面を変える変更は、**基準の読み込み方（reference composition）**で組み立てたシステムプロンプトを使い、変更の前と後を同じ条件で比べます。プラットフォーム（Hermes など）での動きは、そのアダプタの側で別に確かめます。

Powered by SANA OS — https://sana-os.org

## やること・やらないこと

**やること**

- SANA OS の任意のコミットから、ペルソナのシステムプロンプトを組み立てる。`git show` で読むので、比べるコミットをチェックアウトする必要はありません。組み立て方は `personas/README.md` の「Reference composition」のとおりで、ファイルを読み込み順に並べ、空行一つでつなぎ、ほかには何も足しません
- 同じ入力を、同じエンドポイント・モデル・設定で、決めた回数ずつ送る。n 回目はどの版でも同じ seed（`seed + n`）を使うので、変更前と変更後を回ごとに対にして比べられます。版を交互に送るので、サーバー側の揺れも両方に同じように乗ります
- 条件をすべて記録する：比べたコミット、組み立てたプロンプトとそのハッシュ、エンドポイントの設定（鍵は書かない）、すべての送信と応答
- 読むための2つのファイルを書く：`compare.md`（版を並べた応答）と `review.md`（ケースごとの「見る点」と記入欄）

**やらないこと**

- 判定。どちらがよいかは、読む人（作者、またはレビュアー）が決めます。作る役と判定する役を分けるためです
- 正しさの証明。結果は「その条件でこう応答した」という記録です（decision 0003 の 5）

## 準備

1. SANA OS の clone に、比べたいブランチを取得しておく
   ```powershell
   cd D:\GitHub\sana-os
   git fetch origin
   ```
2. `config.example.json` を `config.json` にコピーして、エンドポイントを書く（`config.json` は Git に入りません）
   - llama-server なら `base_url` は `http://<ホスト>:8080/v1`
   - 鍵を環境変数から読むときは、`"api_key"` の代わりに `"api_key_env": "OPENAI_API_KEY"` のように書く
   - `extra_body` は、そのまま要求に足されます（例：思考モードを切る `chat_template_kwargs`）
3. **コンテキスト長**：組み立てたプロンプトは、Coaching で約 4.6 万文字、History で約 6.5 万文字あります。llama-server は `-c 32768` 以上で起動してください。足りないと、サーバーが入力を切り詰めたり、エラーを返したりします。`usage.prompt_tokens` は記録に残ります
4. Python 3.9 以上。標準ライブラリだけで動きます

## 使い方

**まず送らずに確かめる**（組み立てたプロンプトの大きさとハッシュ、実行するケース、送る回数）

```powershell
python sana_eval.py run --sana-os D:\GitHub\sana-os `
  --variant before=origin/main --variant after=origin/wording/invariant-headings `
  --cases cases\pr3-wording.json --dry-run
```

**実行**

```powershell
python sana_eval.py run --sana-os D:\GitHub\sana-os `
  --variant before=origin/main --variant after=origin/wording/invariant-headings `
  --cases cases\pr3-wording.json --config config.json --label pr3
```

結果は `runs\<日時>_pr3\` にできます。

| ファイル | 中身 |
|---|---|
| `run.json` | 比べたコミット、プロンプトのハッシュと文字数、エンドポイントの設定、サーバーが自分について返す情報（`/v1/models`、llama-server なら `/props` のモデルファイル・コンテキスト長・ビルド・チャットテンプレートのハッシュ）、日時、試験装置の版 |
| `prompts\<版>\<ペルソナ>.md` | 実際に送ったシステムプロンプト |
| `transcripts\<ケース>\<版>-<回>.json` | 送った入力と、応答・終了理由・トークン数・時間 |
| `cases.snapshot.json` | 実行したときのケースファイルそのもの。レポートはこれから作る |
| `compare.md` | ケースごとに、回ごと・版ごとの応答を並べたもの。各応答の下に、終了理由（`max_tokens` で切れたかどうか）・トークン数・サーバーが返したモデル名・時間 |
| `review.template.md` | ケースごとの「見る点」と、回ごとの記入欄（ひな形） |
| `review.md` | 記入用。最初に一度だけ、ひな形を写して作る。**以後どのコマンドも上書きしない** |

`review.md` に記入して、`compare.md` と一緒に、その変更の PR にコメントとして貼れば、確認の記録が PR に残ります。`runs\` は Git に入れません（SANA OS の原文とモデルの出力をそのまま含むため）。

そのほか：

- `--only <ケースID> ...`：一部のケースだけ実行する
- `--repeat N`：回数をケースファイルの値から変える
- `python sana_eval.py compose --sana-os ... --ref main --persona history`：一つのペルソナの組み立て結果を表示する（正確な文面を残すときは `--out <ファイル>`。画面への表示では、表示できない文字が置き換わることがあります）
- `python sana_eval.py report runs\<run>`：`compare.md` と `review.template.md` を作り直す（`review.md` はそのまま）
- `python sana_eval.py report runs\<run> --cases <別のケースファイル> --reason "理由"`：実行時とは別の「見る点」で読み直す（再レビュー）。理由は `run.json` に記録され、`compare.rereview-<日時>.md` と `review.rereview-<日時>.md` に分けて書かれます
- 接続できないときは、最初の要求で止まります（3回続けて失敗したときも止まります）。理由は `run.json` に残ります

## ケースファイル

`cases\*.json`。今あるのは次の二つです。

| ファイル | 比べるもの | 中身 |
|---|---|---|
| `pr3-wording.json` | before=`main`（PR2 の後）、after=`wording/invariant-headings` | 見出しの変更で、要件の強さや4原則の扱いの受け取り方が変わらないか |
| `pr4-t7.json` | before=`wording/invariant-headings`、after=`t7/scope` | T7 の適用範囲の変更で、期待どおりに変わるか（聞いてほしいだけのとき、断られたとき、求められたとき、繰り返される依頼、安全フロー） |

```json
{
  "repeat": 3,
  "personas": {
    "general": {"base": "template", "sections": "../fixtures/template-general.md",
                "frameworks": ["frameworks/RBM.md", "frameworks/CPM.md", "frameworks/Value_Formation.md"]}
  },
  "cases": [
    {"id": "...", "persona": "coaching", "purpose": "なぜこのケースか",
     "turns": ["1ターン目", "2ターン目"], "look_for": ["見る点"]}
  ]
}
```

- `persona`：`coaching`・`history`・`reasoning`（SANA OS の読み込み順そのもの）か、`personas` で定義したもの
- Template から作るペルソナは、`sections` に2〜3章だけを書いたファイルを指定します。`fixtures/template-general.md` は、余計な規則を持たない試験用のペルソナです。0〜1章や Core の変更が、ペルソナ自身の規則に隠されずに応答に出るように、足すものを最小にしてあります
- `turns` は利用者の発言の台本です。2ターン目以降も、モデルの応答に関係なく台本どおりに送ります
- 画像を付けるときは `{"text": "...", "images": ["images/tt33.jpg"]}`。パスはケースファイルからの相対パスで、`cases\images\` は Git に入りません
- `source`（任意）は入力の出典、`reference`（任意）は以前の記録です。どちらも `compare.md` と `review.md` に載ります。`reference` は判定の基準ではありません。比べるのは、変更の前と後です
- `TODO:` で始まる発言があるケースは、実行せずに一覧に出します。今は TT33 の回の問いと画像が `TODO:` です。貼り込めば実行されます

## 注意

- `compare.md` には、どちらが変更前かが書いてあります。先入観を避けたいときは、ラベルを伏せて読むか、別の人（レビュアー）に読んでもらってください
- 同じ seed・同じプロンプトでも、実行をまたぐと応答が同じになるとは限りません（2026-10-25 の二回の PR3 試験で、before のプロンプトのハッシュは同じなのに応答が違いました）。比べてよいのは、同じ実行の中の before と after です。実行をまたいで比べるときは、`run.json` のサーバー情報が同じかを先に確かめてください
- `cases.snapshot.json` は、`run.json` に記録したハッシュと照合してから使います。実行後に書き換えられていたら、レポートを作りません。再レビューに使ったケースファイルも、`cases.rereview-<日時>.json` として実行記録の中に残ります

## 検査

`python tests/selftest.py` は、偽の SANA OS リポジトリと偽のエンドポイントを使って、組み立て方・seed の対応・複数ターン・`TODO:` の扱い・鍵を書かないこと、を確かめます。GitHub では push のたびに Python 3.9 と 3.12 で走ります。

## ライセンス

[`LICENSING.md`](LICENSING.md)。道具は MIT、試験用のペルソナ（`fixtures/`）は SANA OS License v1.0 です。
