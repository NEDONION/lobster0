import { useState } from "react";

import type { ProviderList, ProviderSummary, ProviderUpsertInput } from "../common/api";

interface ModelsPanelProps {
  providers: ProviderList | null;
  error: string | null;
  /** Core 是否开放了 providers 写操作；未开放时只渲染只读列表。 */
  canWrite: boolean;
  /** 有回合在跑时禁用全部写操作，与 Core 的忙碌判定保持一致。 */
  busy: boolean;
  onRefresh: () => void;
  onUpsert: (input: ProviderUpsertInput) => Promise<void>;
  onRemove: (id: string) => Promise<void>;
  onSelect: (id: string, model: string) => Promise<void>;
  onSetSecret: (id: string, value: string) => Promise<void>;
}

const RESTART_NOTE = "改动 Provider、模型名或密钥后，需要重启 Core 才会生效。";

export function ModelsPanel({
  providers,
  error,
  canWrite,
  busy,
  onRefresh,
  onUpsert,
  onRemove,
  onSelect,
  onSetSecret,
}: ModelsPanelProps): React.JSX.Element {
  const [adding, setAdding] = useState(false);

  return (
    <section className="models-panel" aria-label="模型与 Provider">
      <header className="models-header">
        <div>
          <h2>模型</h2>
          <p className="models-note">{RESTART_NOTE}</p>
        </div>
        {canWrite ? (
          <div className="models-header-actions">
            <button className="button-secondary" onClick={onRefresh} type="button">
              刷新
            </button>
            <button
              className="button-primary"
              disabled={busy}
              onClick={() => setAdding((value) => !value)}
              type="button"
            >
              {adding ? "取消" : "新增 Provider"}
            </button>
          </div>
        ) : null}
      </header>

      {error ? <p className="panel-error" role="alert">{error}</p> : null}
      {canWrite ? null : (
        <p className="models-note">当前 Core 版本不支持在界面上修改模型配置，以下为只读信息。</p>
      )}

      {adding && canWrite ? (
        <ProviderForm
          busy={busy}
          onCancel={() => setAdding(false)}
          onSubmit={async (input) => {
            await onUpsert(input);
            setAdding(false);
          }}
        />
      ) : null}

      {providers === null ? (
        <p className="models-empty">尚未加载 Provider 列表。</p>
      ) : (
        <ul className="models-list">
          {providers.providers.map((entry) => (
            <ProviderCard
              busy={busy}
              canWrite={canWrite}
              entry={entry}
              key={entry.id}
              model={providers.model}
              onRemove={onRemove}
              onSelect={onSelect}
              onSetSecret={onSetSecret}
              onUpsert={onUpsert}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function ProviderCard({
  entry,
  model,
  canWrite,
  busy,
  onUpsert,
  onRemove,
  onSelect,
  onSetSecret,
}: {
  entry: ProviderSummary;
  model: string;
  canWrite: boolean;
  busy: boolean;
  onUpsert: (input: ProviderUpsertInput) => Promise<void>;
  onRemove: (id: string) => Promise<void>;
  onSelect: (id: string, model: string) => Promise<void>;
  onSetSecret: (id: string, value: string) => Promise<void>;
}): React.JSX.Element {
  const [editing, setEditing] = useState(false);
  const [secret, setSecret] = useState("");
  const [modelDraft, setModelDraft] = useState(model);

  return (
    <li className="models-card" data-provider-id={entry.id}>
      <div className="models-card-head">
        <span className="models-card-id">{entry.id}</span>
        {entry.selected ? <span className="models-badge">默认</span> : null}
        <span className={entry.secretConfigured ? "models-secret-ok" : "models-secret-missing"}>
          密钥{entry.secretConfigured ? "已配置" : "未配置"}
        </span>
      </div>
      <p className="models-card-url">{entry.baseUrl}</p>
      <p className="models-card-meta">
        超时 {entry.timeoutSeconds}s{entry.selected ? ` · 模型 ${model}` : ""}
      </p>

      {canWrite ? (
        <div className="models-card-actions">
          {entry.selected ? null : (
            <button
              className="button-secondary"
              disabled={busy}
              onClick={() => {
                void onSelect(entry.id, modelDraft.trim() || model);
              }}
              type="button"
            >
              设为默认
            </button>
          )}
          <button
            className="button-secondary"
            disabled={busy}
            onClick={() => setEditing((value) => !value)}
            type="button"
          >
            {editing ? "收起" : "编辑"}
          </button>
          {/* 删除当前默认项会在配置里留下悬空引用，这里连入口都不给。 */}
          {entry.selected ? null : (
            <button
              className="button-secondary models-remove"
              disabled={busy}
              onClick={() => {
                void onRemove(entry.id);
              }}
              type="button"
            >
              删除
            </button>
          )}
        </div>
      ) : null}

      {canWrite ? (
        <div className="models-card-secret">
          <label className="models-field">
            <span>API Key</span>
            {/* 不设 value：密钥永不从 Core 回流，输入框只用于写入。 */}
            <input
              autoComplete="off"
              onChange={(event) => setSecret(event.target.value)}
              placeholder={entry.secretConfigured ? "已配置，填写以覆盖" : "尚未配置"}
              type="password"
            />
          </label>
          <button
            className="button-primary"
            disabled={busy || secret.trim().length === 0}
            onClick={() => {
              const value = secret.trim();
              // 提交后立刻清空本地 state，不让明文在 Renderer 里多留一刻。
              setSecret("");
              void onSetSecret(entry.id, value);
            }}
            type="button"
          >
            保存密钥
          </button>
        </div>
      ) : null}

      {editing && canWrite ? (
        <div className="models-card-editor">
          <ProviderForm
            busy={busy}
            initial={entry}
            onCancel={() => setEditing(false)}
            onSubmit={async (input) => {
              await onUpsert(input);
              setEditing(false);
            }}
          />
          <label className="models-field">
            <span>模型名</span>
            <input
              onChange={(event) => setModelDraft(event.target.value)}
              type="text"
              value={modelDraft}
            />
          </label>
          <button
            className="button-secondary"
            disabled={busy || modelDraft.trim().length === 0}
            onClick={() => {
              void onSelect(entry.id, modelDraft.trim());
            }}
            type="button"
          >
            应用模型名
          </button>
        </div>
      ) : null}
    </li>
  );
}

function ProviderForm({
  initial,
  busy,
  onSubmit,
  onCancel,
}: {
  initial?: ProviderSummary;
  busy: boolean;
  onSubmit: (input: ProviderUpsertInput) => Promise<void>;
  onCancel: () => void;
}): React.JSX.Element {
  const [id, setId] = useState(initial?.id ?? "");
  const [baseUrl, setBaseUrl] = useState(initial?.baseUrl ?? "https://");
  const [timeoutSeconds, setTimeoutSeconds] = useState(String(initial?.timeoutSeconds ?? 120));

  const timeout = Number.parseInt(timeoutSeconds, 10);
  const valid =
    /^[a-z0-9][a-z0-9_-]{0,31}$/.test(id)
    && /^https?:\/\/\S+$/.test(baseUrl)
    && Number.isSafeInteger(timeout)
    && timeout >= 1
    && timeout <= 3600;

  return (
    <div className="models-form">
      <label className="models-field">
        <span>标识</span>
        <input
          disabled={initial !== undefined}
          onChange={(event) => setId(event.target.value)}
          placeholder="openrouter"
          type="text"
          value={id}
        />
      </label>
      <label className="models-field">
        <span>Base URL</span>
        <input onChange={(event) => setBaseUrl(event.target.value)} type="text" value={baseUrl} />
      </label>
      <label className="models-field">
        <span>超时（秒）</span>
        <input
          onChange={(event) => setTimeoutSeconds(event.target.value)}
          type="text"
          value={timeoutSeconds}
        />
      </label>
      <div className="models-form-actions">
        <button
          className="button-primary"
          disabled={busy || !valid}
          onClick={() => {
            void onSubmit({ id, baseUrl, timeoutSeconds: timeout });
          }}
          type="button"
        >
          保存
        </button>
        <button className="button-secondary" onClick={onCancel} type="button">
          取消
        </button>
      </div>
      <p className="models-note">
        标识只能用小写字母、数字、下划线和连字符；密钥变量名由 Core 从标识推导。
      </p>
    </div>
  );
}
