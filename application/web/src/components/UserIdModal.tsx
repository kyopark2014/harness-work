import { FormEvent } from "react";
import { createPortal } from "react-dom";
import { formatBrandTitle } from "../formatBrandTitle";

interface Props {
  onSubmit: (username: string, password: string) => void;
  error?: string | null;
  projectName?: string | null;
  loading?: boolean;
}

export function UserIdModal({ onSubmit, error, projectName, loading }: Props) {
  const title = formatBrandTitle(projectName ?? "agent");

  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (loading) return;
    const form = new FormData(e.currentTarget);
    const username = String(form.get("username") ?? "").trim();
    const password = String(form.get("password") ?? "");
    if (!username || !password) return;
    onSubmit(username, password);
  }

  return createPortal(
    <div className="auth-screen">
      <div className="auth-backdrop" aria-hidden="true" />
      <div
        className="modal-overlay auth-overlay"
        role="dialog"
        aria-modal="true"
        aria-labelledby="login-title"
      >
        <div className="auth-card">
          <div className="auth-card-glow" aria-hidden="true" />
          <div className="auth-brand-mark" aria-hidden="true" />
          <h2 id="login-title">{title}</h2>
          <p className="auth-subtitle">Cognito 계정으로 로그인하세요.</p>
          {error && <p className="modal-error">{error}</p>}
          <form className="local-auth-bypass" onSubmit={handleSubmit}>
            <label className="auth-field" htmlFor="login-username">
              <span className="auth-field-label">ID</span>
              <input
                id="login-username"
                name="username"
                placeholder="예: admin"
                autoComplete="username"
                autoFocus
                required
                disabled={loading}
              />
            </label>
            <label className="auth-field" htmlFor="login-password">
              <span className="auth-field-label">Password</span>
              <input
                id="login-password"
                name="password"
                type="password"
                placeholder="비밀번호"
                autoComplete="current-password"
                required
                disabled={loading}
              />
            </label>
            <button type="submit" className="auth-primary-btn" disabled={loading}>
              {loading ? "로그인 중…" : "로그인"}
            </button>
          </form>
        </div>
      </div>
    </div>,
    document.body,
  );
}
