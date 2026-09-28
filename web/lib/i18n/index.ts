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
    /** Weekly timesheets: your own hours, which every role may fill in. */
    timesheets: string;
    /** The document list: what this caller may read, and where uploads are filed. */
    documents: string;
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
    /** Timesheet refusals the employee can act on, so the screen must name them. */
    timesheet_not_editable: string;
    timesheet_not_yours: string;
    timesheet_week_not_monday: string;
    timesheet_entry_minutes_invalid: string;
    timesheet_entry_outside_project_dates: string;
    timesheet_entry_project_not_recordable: string;
    timesheet_copy_source_invalid: string;
    timesheet_copy_target_not_empty: string;
    timesheet_submission_refused: string;
    /** Ticket 29's refusals: the two locks and the supplement's own rules. Each has a
     *  remedy, so each is a sentence the employee can act on rather than a rule. */
    timesheet_week_locked: string;
    timesheet_week_closed: string;
    timesheet_supplement_not_locked: string;
    timesheet_supplement_open: string;
    timesheet_supplement_invalid: string;
    timesheet_entry_is_reversal: string;
    /** Ticket 30: the report's period was unusable, so the remedy is another period
     *  rather than another request. */
    timesheet_report_range_invalid: string;
    project_task_not_recordable: string;
    /** Document refusals the uploader can act on: choose another file, or open the one
     *  they already have. The type refusal and the ceiling both belong to "pick a
     *  file", which is the moment the uploader can still do something about it. */
    document_upload_type_unsupported: string;
    document_upload_too_large: string;
    document_upload_empty: string;
    document_duplicate: string;
    document_not_ready: string;
    document_reprocess_unsupported: string;
    document_file_missing: string;
  };
  documents: {
    title: string;
    intro: string;
    /** The list's heading, with how many of the total are on screen. */
    listHeading: string;
    empty: string;
    emptyHint: string;
    loading: string;
    error: string;
    retry: string;
    /** The statuses, as words: colour alone never carries a status (§5). */
    status: {
      processing: string;
      ready: string;
      failed: string;
      archived: string;
    };
    /** How far along the pipeline is, with both numbers interpolated. */
    progress: string;
    /** Accessible name of the progress element; the visible text is `progress`. */
    progressLabel: string;
    /** What the pipeline found, when it found something. */
    parsed: string;
    /** What it found instead, when it found nothing — the ticket's own sentence. */
    failureReason: string;
    /** A company document, which belongs to the organisation rather than to a person. */
    companyDocument: string;
    clearance: string;
    department: string;
    owner: string;
    /** Shown where an owner would be, for a document that has none. */
    noDepartment: string;
    uploadedAt: string;
    download: string;
    reprocess: string;
    reprocessing: string;
    upload: {
      heading: string;
      description: string;
      fileLabel: string;
      fileHint: string;
      titleLabel: string;
      titlePlaceholder: string;
      departmentLabel: string;
      departmentPlaceholder: string;
      clearanceLabel: string;
      categoryLabel: string;
      tagsLabel: string;
      tagsHint: string;
      submit: string;
      submitting: string;
      required: string;
      /** The five formats, named once so the hint and the refusal agree. */
      formats: string;
      accepted: string;
      /** Shown after a successful upload, before the job has parsed it. */
      queued: string;
    };
  };
  timesheets: {
    title: string;
    intro: string;
    /** The week being shown, with the range interpolated. */
    weekOf: string;
    previousWeek: string;
    nextWeek: string;
    thisWeek: string;
    copyPrevious: string;
    copying: string;
    submit: string;
    submitting: string;
    /** Shown in place of the submit button once the week is filed. */
    submittedOn: string;
    /** The statuses, as words. Colour alone never carries a status (§5). */
    status: {
      draft: string;
      pending: string;
      approved: string;
      rejected: string;
    };
    /** Says why a filed week cannot be edited, beside the status. */
    lockedHint: string;
    rejectedHint: string;
    /** The per-entry ceiling, stated so a refusal is predicted rather than discovered. */
    minutesHint: string;
    /** Says a row will be billed. A word, because a colour alone cannot carry it (§5). */
    billable: string;
    /** A day's expectation, with the duration interpolated. */
    expected: string;
    /** A day nobody has a schedule for: not the same fact as expecting nothing. */
    expectedUnknown: string;
    /** A holiday, which is a rest day rather than a day somebody skipped. */
    holiday: string;
    dayTotal: string;
    weekTotal: string;
    weekExpected: string;
    overBudget: string;
    overBudgetDay: string;
    noEntries: string;
    emptyHint: string;
    loading: string;
    error: string;
    retry: string;
    /** The grid's caption and column headers. */
    caption: string;
    /** Accessible names for the grid controls. */
    addEntry: string;
    removeEntry: string;
    saving: string;
    entryForm: {
      heading: string;
      project: string;
      task: string;
      projectPlaceholder: string;
      taskPlaceholder: string;
      minutes: string;
      note: string;
      notePlaceholder: string;
      add: string;
      cancel: string;
      projectRequired: string;
      taskRequired: string;
      minutesRequired: string;
      minutesRange: string;
    };
    /** The narrow-viewport downgrade (design system §7), not a squeezed grid. */
    desktopOnly: {
      title: string;
      body: string;
      alternative: string;
    };
    history: {
      heading: string;
      round: string;
      level: string;
      decision: string;
      comment: string;
      decidedAt: string;
      empty: string;
      decisions: {
        approved: string;
        rejected: string;
        returned: string;
        pending: string;
        skipped: string;
      };
    };
    /** The lock (ticket 29): which weeks are closed, and why. */
    lock: {
      locked: string;
      lockedHint: string;
      closed: string;
      /** The window's length is interpolated: it is the same number the API refuses with. */
      closedHint: string;
      /** How long this week may still be corrected, with the count interpolated. */
      supplementWindow: string;
    };
    /** The supplementary submission: one correction per locked entry. */
    supplement: {
      heading: string;
      intro: string;
      open: string;
      openHint: string;
      entryLabel: string;
      newMinutes: string;
      remove: string;
      removeHint: string;
      unchanged: string;
      submit: string;
      submitting: string;
      cancel: string;
      nothingChanged: string;
      invalidMinutes: string;
      opened: string;
      inFlight: string;
      inFlightHint: string;
      linkOriginal: string;
      sheetsHeading: string;
      sheetOriginal: string;
      sheetSupplement: string;
    };
    /** How a corrected day and task read: recorded, adjusted, net. */
    adjustments: {
      heading: string;
      day: string;
      task: string;
      gross: string;
      reversed: string;
      net: string;
      reversalRow: string;
      reversalOf: string;
      replacement: string;
    };    tasks: {
      heading: string;
      empty: string;
    };
  };
  footer: {
    milestone: string;
  };
};

const DICTIONARIES: Record<Locale, Dictionary> = { es, en };

export function getDictionary(locale: Locale): Dictionary {
  return DICTIONARIES[locale] ?? DICTIONARIES[DEFAULT_LOCALE];
}
