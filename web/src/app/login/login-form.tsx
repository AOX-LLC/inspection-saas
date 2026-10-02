"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { BrandMark } from "@/components/icons";
import { Alert } from "@/components/ui";
import { useLoad } from "@/components/use-load";
import { api } from "@/lib/api";
import { describeLoginFailure } from "@/lib/messages";
import { safeNext } from "@/lib/redirect";

export function LoginForm() {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Only a demo-mode API has this route; anywhere else it is a 404 and the list stays hidden.
  const demo = useLoad(api.demoAccounts, "demo-accounts");
  const accounts = demo.status === "ready" ? demo.data : null;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      await api.login(email.trim(), password);
      router.replace(next);
    } catch (error) {
      setProblem(describeLoginFailure(error));
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <div className="login-brand">
        <BrandMark />
        <h1>Sign in to Inspection</h1>
        <p className="muted">Upload site photos and review them.</p>
      </div>

      <form className="card card-pad form" onSubmit={submit}>
        {problem ? <Alert tone="danger">{problem}</Alert> : null}
        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            className="input"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
              setPicked(null);
            }}
          />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            className="input"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>

      {accounts ? (
        <section className="card card-pad form" aria-labelledby="demo-title">
          <div>
            <h2 id="demo-title">Demo accounts</h2>
            <p className="muted small">
              Synthetic users, shown because this is a demo. Pick one to fill in the form, then sign in.
            </p>
          </div>
          <div className="demo-list">
            {accounts.accounts.map((account) => (
              <button
                key={account.email}
                type="button"
                className="demo-account"
                aria-pressed={picked === account.email}
                onClick={() => {
                  setEmail(account.email);
                  setPassword(accounts.password);
                  setPicked(account.email);
                }}
              >
                <span className="list-row-title">{account.display_name}</span>
                <span className="muted small">
                  {account.email} · {account.description}
                </span>
              </button>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}
