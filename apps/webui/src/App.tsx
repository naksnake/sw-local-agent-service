import { PRODUCT_NAME } from "./branding";
import { cn } from "./lib/utils";

/**
 * Phase 0 shell. The real pages (CLAUDE.md §9) start with the login page in Phase 1.
 * Copy follows §9: sentences, and it says what will happen.
 */
export function App(): JSX.Element {
  return (
    <main className={cn("min-h-screen bg-neutral-100 p-8 text-neutral-900")}>
      <h1 className="text-xl font-semibold">{PRODUCT_NAME}</h1>
      <p className="mt-2 max-w-prose">
        This is the Phase 0 skeleton. The login page arrives in Phase 1; until then this
        page only confirms that the web interface builds and serves.
      </p>
    </main>
  );
}
