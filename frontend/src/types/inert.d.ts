import "react";

// React 18's types do not include the standard `inert` attribute. An empty string renders it as
// a present attribute (`inert=""`); `undefined` omits it. Used by the mobile navigation drawer.
declare module "react" {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface HTMLAttributes<T> {
    inert?: "" | undefined;
  }
}
