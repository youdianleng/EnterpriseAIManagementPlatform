/**
 * Joins class names, dropping falsy values.
 *
 * Deliberately not a dependency: the whole implementation is this one line, and
 * a class-merging library would be a shallow module here.
 */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
