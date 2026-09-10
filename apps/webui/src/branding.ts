/**
 * Product naming (CLAUDE.md §0.1). Mirrors `slas_kernel.branding`; if the product is renamed,
 * change both constants and run the find-and-replace in CLAUDE.md.
 */
export const PRODUCT_NAME = "SW Local Agent Service";
export const SLUG = "slas";
export const AGENT_NAMES = ["Coding Agent", "Validation Agent", "Factory Agent"] as const;
export const PAGES = [
  "Home",
  "Coding",
  "Validation",
  "Factory",
  "Runs",
  "Tickets",
  "Models",
  "Skills",
  "Knowledge",
  "Admin",
] as const;
