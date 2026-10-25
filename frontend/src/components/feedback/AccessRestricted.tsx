import { ReactNode, useId } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";

/** Shown when the signed-in user is not allowed to see a page. It is deliberately not an empty
 * state, a zero, or an offline message: the data exists, the user just may not read it. The
 * backend still enforces every request; this only explains the refusal up front. */
export function AccessRestricted({
  what,
  permission,
  headingLevel = 1,
}: {
  what: string;
  permission?: string;
  /** 1 when this replaces a whole page; 2 or 3 when it sits inside a page or a section that already has its own headings. */
  headingLevel?: 1 | 2 | 3;
}) {
  const Heading = headingLevel === 1 ? "h1" : headingLevel === 2 ? "h2" : "h3";
  const titleId = useId();
  // Page-level: a labelled region. Inside a page or a section it is a plain block, so several of them
  // on one page do not become identically named landmarks.
  const Container = headingLevel === 1 ? "section" : "div";
  return (
    <Container aria-labelledby={headingLevel === 1 ? titleId : undefined} className="max-w-xl rounded-sm border border-line-subtle bg-surface-panel p-4">
      <Heading id={titleId} className="text-lg font-semibold text-fg-primary">
        Access restricted
      </Heading>
      <p className="mt-2 text-sm text-fg-secondary">
        Your account cannot view {what}.
        {permission ? (
          <>
            {" "}It needs the <code className="rounded-sm bg-surface-page px-1">{permission}</code> permission.
          </>
        ) : null}{" "}
        Ask an administrator if you need access.
      </p>
    </Container>
  );
}

/** Renders children only for users holding `permission`; otherwise explains the restriction. */
export function RequirePermission({ permission, what, children }: { permission: string; what: string; children: ReactNode }) {
  const allowed = useHasPermission(permission);
  return allowed ? <>{children}</> : <AccessRestricted what={what} permission={permission} />;
}
