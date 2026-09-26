import { DEFAULT_LOCALE, type Locale } from "./config";
import { en } from "./messages/en";
import { es } from "./messages/es";

/**
 * Shape shared by every locale file.
 *
 * Locale files are typed against this, so a key present in Spanish but missing
 * in English fails the type check instead of rendering an empty string.
 */
export type Dictionary = {
  app: {
    name: string;
    tagline: string;
  };
  language: {
    label: string;
  };
  nav: {
    styleGuide: string;
    home: string;
    /** The notification centre, which every signed-in role can open. */
    notifications: string;
    /** Accessible name of the signed-in navigation landmark. */
    main: string;
  };
  backend: {
    heading: string;
    loading: string;
    connected: string;
    failed: string;
    description: string;
    fields: {
      name: string;
      version: string;
      environment: string;
      apiPrefix: string;
    };
    hint: string;
  };
  styleGuide: {
    title: string;
    intro: string;
    colours: {
      title: string;
      description: string;
      neutrals: string;
      primary: string;
      semantic: string;
      contrastNote: string;
    };
    typography: {
      title: string;
      description: string;
      body: string;
      secondary: string;
      sectionHeading: string;
      pageHeading: string;
      tabularTitle: string;
      tabularNote: string;
    };
    spacing: {
      title: string;
      description: string;
    };
    buttons: {
      title: string;
      description: string;
      primary: string;
      secondary: string;
      ghost: string;
      danger: string;
      small: string;
      disabled: string;
    };
    states: {
      title: string;
      description: string;
      info: string;
      success: string;
      warning: string;
      danger: string;
      neutral: string;
    };
    forms: {
      title: string;
      description: string;
      nameLabel: string;
      namePlaceholder: string;
      nameHint: string;
      emailLabel: string;
      emailError: string;
      departmentLabel: string;
      departmentPlaceholder: string;
      departmentOption1: string;
      departmentOption2: string;
      submit: string;
    };
    table: {
      title: string;
      description: string;
      caption: string;
      employee: string;
      department: string;
      hours: string;
      empty: string;
    };
    dialog: {
      title: string;
      description: string;
      open: string;
      body: string;
      cancel: string;
      confirm: string;
      close: string;
    };
  };
  auth: {
    login: {
      title: string;
      intro: string;
      usernameLabel: string;
      usernamePlaceholder: string;
      passwordLabel: string;
      passwordPlaceholder: string;
      submit: string;
      submitting: string;
      /** Required-field messages; the API's own length rule is not restated. */
      usernameRequired: string;
      passwordRequired: string;
      /** Shown with the remaining lockout time interpolated. */
      lockedOut: string;
    };
    changePassword: {
      title: string;
      /** Why the person is here and cannot be anywhere else. */
      intro: string;
      currentLabel: string;
      newLabel: string;
      confirmLabel: string;
      submit: string;
      submitting: string;
      signOut: string;
      currentRequired: string;
      newRequired: string;
      confirmRequired: string;
      /** Checked locally so a typo cannot silently become the new password. */
      mismatch: string;
      signOutHint: string;
    };
    policy: {
      title: string;
      /** The rule itself is read from `/auth/password-policy`, never restated. */
      intro: string;
      minimum: string;
      contain: string;
      classes: {
        lower: string;
        upper: string;
        digit: string;
        special: string;
      };
    };
    violations: {
      too_short: string;
      missing_lower: string;
      missing_upper: string;
      missing_digit: string;
      missing_special: string;
      unknown: string;
    };
    shell: {
      signedInAs: string;
      nameLabel: string;
      usernameLabel: string;
      signOut: string;
      signingOut: string;
      sessionUnavailable: string;
      retry: string;
    };
  };
  notifications: {
    title: string;
    intro: string;
    /** Shown on a notification the caller has not read yet. */
    unreadLabel: string;
    /** Section heading: how much of the list is still unread. */
    unreadSummary: string;
    /** Shown in place of the button once it has been read. */
    readLabel: string;
    markRead: string;
    markingRead: string;
    markAllRead: string;
    markingAllRead: string;
    /** Accessible name of the header badge; `{count}` is the unread number. */
    unreadBadge: string;
    empty: string;
    emptyHint: string;
    loading: string;
    error: string;
    retry: string;
    /** Metadata labels; the values themselves are formatted by `lib/format`. */
    received: string;
    level: string;
    round: string;
    /**
     * The titles, keyed by the `title_key` the API stores. A key the frontend has
     * not learned yet degrades to `unknown` rather than to a blank line.
     */
    titles: {
      awaitingDecision: string;
      approved: string;
      rejected: string;
      returned: string;
      withdrawn: string;
      unknown: string;
    };
  };
  errors: {
    account_invalid_credentials: string;
    account_locked: string;
    account_disabled: string;
    account_password_policy: string;
    account_password_reused: string;
    notification_not_yours: string;
    session_invalid: string;
    password_change_required: string;
    validation_failed: string;
    internal_error: string;
    service_unavailable: string;
  };
  footer: {
    milestone: string;
  };
};

const DICTIONARIES: Record<Locale, Dictionary> = { es, en };

export function getDictionary(locale: Locale): Dictionary {
  return DICTIONARIES[locale] ?? DICTIONARIES[DEFAULT_LOCALE];
}
