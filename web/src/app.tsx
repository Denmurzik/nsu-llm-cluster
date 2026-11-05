import { useCallback, useEffect, useState } from "react";
import { Route, Routes, Navigate } from "react-router-dom";
import { api, ApiError, onUnauthorized } from "./api";
import { MeContext } from "./auth";
import { Shell } from "./components/Shell";
import { AdminBackups } from "./pages/AdminBackups";
import { AdminDeploymentEdit } from "./pages/AdminDeploymentEdit";
import { AdminDeploymentNew, AdminDeployments } from "./pages/AdminDeployments";
import { AdminLocalModels } from "./pages/AdminLocalModels";
import { AdminModelCache } from "./pages/AdminModelCache";
import { AdminMonitoring } from "./pages/AdminMonitoring";
import { AdminNodes } from "./pages/AdminNodes";
import { AdminSettings } from "./pages/AdminSettings";
import { AdminStatus } from "./pages/AdminStatus";
import { AdminUsage } from "./pages/AdminUsage";
import { AdminAudit } from "./pages/AdminAudit";
import { AdminUsers } from "./pages/AdminUsers";
import { Forbidden, RequireAdmin } from "./components/Guard";
import { Keys } from "./pages/Keys";
import { Login } from "./pages/Login";
import { Models } from "./pages/Models";
import { Playground } from "./pages/Playground";
import { Quickstart } from "./pages/Quickstart";
import { Usage } from "./pages/Usage";
import type { Me } from "./types";

type Session = { kind: "loading" } | { kind: "anon" } | { kind: "error"; message: string } | { kind: "user"; me: Me };

export function App() {
  const [session, setSession] = useState<Session>({ kind: "loading" });

  const load = useCallback(() => {
    api
      .me()
      .then((me) => setSession({ kind: "user", me }))
      .catch((e: ApiError) =>
        setSession(e.status === 401 ? { kind: "anon" } : { kind: "error", message: e.detail || "Сервис недоступен" }),
      );
  }, []);

  useEffect(() => {
    load();
    return onUnauthorized(() => setSession({ kind: "anon" }));
  }, [load]);

  if (session.kind === "loading") return null;
  if (session.kind === "anon") return <Login />;
  if (session.kind === "error") {
    return (
      <div className="center-screen" role="alert">
        <p>{session.message}</p>
        <button type="button" className="btn" onClick={load}>
          Повторить
        </button>
      </div>
    );
  }

  return (
    <MeContext.Provider value={session.me}>
      <Shell>
        <Routes>
          <Route path="/" element={<Navigate to="/models" replace />} />
          <Route path="/models" element={<Models />} />
          <Route path="/keys" element={<Keys />} />
          <Route path="/quickstart" element={<Quickstart />} />
          <Route path="/playground" element={<Playground />} />
          <Route path="/usage" element={<Usage />} />
          <Route
            path="/admin/deployments"
            element={
              <RequireAdmin>
                <AdminDeployments />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/deployments/new"
            element={
              <RequireAdmin>
                <AdminDeploymentNew />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/deployments/:id"
            element={
              <RequireAdmin>
                <AdminDeploymentEdit />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/local-models"
            element={
              <RequireAdmin>
                <AdminLocalModels />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/cache"
            element={
              <RequireAdmin>
                <AdminModelCache />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/nodes"
            element={
              <RequireAdmin>
                <AdminNodes />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/status"
            element={
              <RequireAdmin>
                <AdminStatus />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/monitoring"
            element={
              <RequireAdmin>
                <AdminMonitoring />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/users"
            element={
              <RequireAdmin>
                <AdminUsers />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/backups"
            element={
              <RequireAdmin>
                <AdminBackups />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/usage"
            element={
              <RequireAdmin>
                <AdminUsage />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/settings"
            element={
              <RequireAdmin>
                <AdminSettings />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/audit"
            element={
              <RequireAdmin>
                <AdminAudit />
              </RequireAdmin>
            }
          />
          <Route path="*" element={<Forbidden title="Страница не найдена" text="Такого адреса в интерфейсе нет" />} />
        </Routes>
      </Shell>
    </MeContext.Provider>
  );
}
