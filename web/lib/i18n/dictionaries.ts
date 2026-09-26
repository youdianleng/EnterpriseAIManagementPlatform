/**
 * Re-export so consumers can `import { Dictionary } from "@/lib/i18n/dictionaries"`.
 *
 * The type itself lives in `./index` next to the dictionary map, because locale
 * files import this path and a definition here would create a cycle.
 */
export type { Dictionary } from "./index";
