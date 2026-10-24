import { ReactNode } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";

/** Shown when the signed-in user is not allowed to see a page. It is deliberately not an empty
 * state, a zero, or an offline message: the data exists, the user just may not read it. The
 * backend still enforces every request; this only explains the refusal up front. */
export function AccessRestricted({ what, permission }: { what: string; permission?: string }) {
  return (
    <section aria-labelledby="access-restricted-title" className="max-w-xl rounded-sm border border-line-subtle bg-surface-panel p-4">
      <h1 id="access-restricted-title" className="text-lg font-semibold text-fg-primary">
        Access restricted
      </h1>
      <p className="mt-2 text-sm text-fg-secondary">
        Your account cannot view {what}.
        {permission ? (
          <>
            {" "}It needs the <code className="rounded-sm bg-black/30 px-1">{permission}</code> permission.
          </>
        ) : null}{" "}
        Ask an administrator if you need access.
      </p>
    </section>
  );
}

/** Renders children only for users holding `permission`; otherwise explains the restriction. */
export function RequirePermission({ permission, what, children }: { permission: string; what: string; children: ReactNode }) {
  const allowed = useHasPermission(permission);
  return allowed ? <>{children}</> : <AccessRestricted what={what} permission={permission} />;
}
