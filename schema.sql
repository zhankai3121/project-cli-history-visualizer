-- CLI History Visualizer — schema
-- 專案 key 一律用真實路徑（slug 不可逆，見 docs/jsonl-schema.md）

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- exists_on_disk 是軟刪除：資料夾不見了就設 0，UI 預設隱藏，但 CLI 歷史留著。
-- 真的把 row 刪掉會連帶毀掉那個專案所有的 prompt 歷史，不值得。
CREATE TABLE IF NOT EXISTS project (
    id             INTEGER PRIMARY KEY,
    real_path      TEXT NOT NULL UNIQUE,
    display_name   TEXT NOT NULL,
    first_seen     TEXT,
    last_seen      TEXT,
    session_count  INTEGER NOT NULL DEFAULT 0,
    prompt_count   INTEGER NOT NULL DEFAULT 0,
    exists_on_disk INTEGER NOT NULL DEFAULT 0,
    has_history    INTEGER NOT NULL DEFAULT 0,
    is_git         INTEGER NOT NULL DEFAULT 0,
    disk_mtime     TEXT,
    vanished_at    TEXT,
    -- git 聯動：驗證 prompt 講的事情有沒有真的落地
    git_branch     TEXT,
    git_last_ts    TEXT,
    git_last_msg   TEXT,
    git_dirty      INTEGER,
    git_commits    INTEGER,
    -- 容器目錄（專案根本身與其祖先），不是專案。UI 預設隱藏
    is_container   INTEGER NOT NULL DEFAULT 0,
    -- 由資料夾掃描發現（可能從未跑過 CLI）
    is_scanned     INTEGER NOT NULL DEFAULT 0,
    -- 作業系統目錄（C:\Windows、/usr…），不是專案
    is_system      INTEGER NOT NULL DEFAULT 0
);

-- 設定（目前只存 project_roots）
CREATE TABLE IF NOT EXISTS app_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session (
    id               TEXT PRIMARY KEY,
    project_id       INTEGER REFERENCES project(id),
    started_at       TEXT,
    ended_at         TEXT,
    prompt_count     INTEGER NOT NULL DEFAULT 0,
    title            TEXT,
    transcript_state TEXT NOT NULL DEFAULT 'gone',   -- live | gone
    transcript_path  TEXT,
    tool             TEXT NOT NULL DEFAULT 'claude'  -- claude | codex
);

CREATE TABLE IF NOT EXISTS prompt (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_id INTEGER REFERENCES project(id),
    ts         TEXT NOT NULL,                        -- ISO-8601 UTC
    seq        INTEGER NOT NULL DEFAULT 0,
    text       TEXT NOT NULL,
    is_slash   INTEGER NOT NULL DEFAULT 0,
    source     TEXT NOT NULL,                        -- history | transcript
    pasted     TEXT,
    tool       TEXT NOT NULL DEFAULT 'claude',       -- claude | codex
    UNIQUE(session_id, ts, text)
);
CREATE INDEX IF NOT EXISTS ix_prompt_project ON prompt(project_id, ts);
CREATE INDEX IF NOT EXISTS ix_prompt_session ON prompt(session_id, seq);
CREATE INDEX IF NOT EXISTS ix_prompt_ts      ON prompt(ts);

-- 一個 turn = 一則真人 prompt 之後、下一則之前的 assistant 活動總和
CREATE TABLE IF NOT EXISTS turn (
    id                INTEGER PRIMARY KEY,
    session_id        TEXT NOT NULL,
    project_id        INTEGER REFERENCES project(id),
    prompt_id         INTEGER REFERENCES prompt(id),
    ts                TEXT,
    assistant_summary TEXT,
    tools_json        TEXT,
    has_error         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_turn_session ON turn(session_id, ts);

CREATE TABLE IF NOT EXISTS file_touch (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_id INTEGER REFERENCES project(id),
    ts         TEXT,
    path       TEXT NOT NULL,
    verb       TEXT NOT NULL                         -- Edit | Write | NotebookEdit
);
CREATE INDEX IF NOT EXISTS ix_touch_project ON file_touch(project_id, path);

CREATE TABLE IF NOT EXISTS command_run (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_id INTEGER REFERENCES project(id),
    ts         TEXT,
    command    TEXT NOT NULL,
    kind       TEXT                                  -- git | test | build | install | other
);

CREATE TABLE IF NOT EXISTS commit_ref (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_id INTEGER REFERENCES project(id),
    ts         TEXT,
    message    TEXT NOT NULL
);

-- 進度訊號：CLI 自己寫的摘要 + 使用者手寫的進度檔，統一收在這
CREATE TABLE IF NOT EXISTS progress_signal (
    id         INTEGER PRIMARY KEY,
    session_id TEXT,
    project_id INTEGER REFERENCES project(id),
    ts         TEXT,
    kind       TEXT NOT NULL,   -- away_summary | compact_summary | ai_title
                                -- | last_prompt | cost_state | memory_file
    goal       TEXT,
    state      TEXT,            -- 中文 away_summary 的前半是「現況」不是目標，分開存
    next_step  TEXT,
    body       TEXT,
    origin     TEXT,            -- 給 UI 顯示來源用，如 'away_summary' / 'brain.md'
    UNIQUE(session_id, kind, ts, body)
);
CREATE INDEX IF NOT EXISTS ix_signal_project ON progress_signal(project_id, ts);

-- 增量索引游標（jsonl 是 append-only，可直接 seek）
CREATE TABLE IF NOT EXISTS scan_state (
    path        TEXT PRIMARY KEY,
    mtime       REAL,
    size        INTEGER,
    last_offset INTEGER NOT NULL DEFAULT 0
);

-- ── FTS5（trigram：中文必需；注意 2 字查詢靜默回 0，查詢層要 fallback 到 LIKE）──
CREATE VIRTUAL TABLE IF NOT EXISTS prompt_fts
    USING fts5(text, content='prompt', content_rowid='id', tokenize='trigram');

CREATE TRIGGER IF NOT EXISTS prompt_ai AFTER INSERT ON prompt BEGIN
    INSERT INTO prompt_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS prompt_ad AFTER DELETE ON prompt BEGIN
    INSERT INTO prompt_fts(prompt_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS prompt_au AFTER UPDATE ON prompt BEGIN
    INSERT INTO prompt_fts(prompt_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO prompt_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS turn_fts
    USING fts5(assistant_summary, content='turn', content_rowid='id', tokenize='trigram');

CREATE TRIGGER IF NOT EXISTS turn_ai AFTER INSERT ON turn BEGIN
    INSERT INTO turn_fts(rowid, assistant_summary) VALUES (new.id, new.assistant_summary);
END;
CREATE TRIGGER IF NOT EXISTS turn_ad AFTER DELETE ON turn BEGIN
    INSERT INTO turn_fts(turn_fts, rowid, assistant_summary) VALUES ('delete', old.id, old.assistant_summary);
END;
