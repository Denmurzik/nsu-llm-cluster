import type { LucideIcon, LucideProps } from "lucide-react";
import { CircleAlert, TriangleAlert, Info } from "lucide-react";
import type { ReactNode } from "react";
import type { ApiError } from "../api";

/** One icon set, one size and stroke, text-role colour. */
export function Ic({ icon: Icon, ...rest }: { icon: LucideIcon } & LucideProps) {
  return <Icon size={16} strokeWidth={1.5} className="ic" aria-hidden="true" {...rest} />;
}

export function Loading() {
  return (
    <p className="loading" role="status">
      Загрузка
    </p>
  );
}

export function Notice({
  kind = "info",
  children,
  alert = false,
}: {
  kind?: "info" | "warn" | "error";
  children: ReactNode;
  alert?: boolean;
}) {
  const icon = kind === "error" ? CircleAlert : kind === "warn" ? TriangleAlert : Info;
  return (
    <div className={`notice ${kind === "info" ? "" : kind}`} role={alert ? "alert" : undefined}>
      <Ic icon={icon} />
      <div>{children}</div>
    </div>
  );
}

export function ErrorNotice({ error }: { error: ApiError | Error | string | null | undefined }) {
  if (!error) return null;
  const text = typeof error === "string" ? error : "detail" in error ? (error as ApiError).detail : error.message;
  return (
    <Notice kind="error" alert>
      {text}
    </Notice>
  );
}

export function PageHead({ title, lead, actions }: { title: string; lead?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {lead && <p className="lead">{lead}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}
