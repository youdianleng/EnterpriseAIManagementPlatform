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
    /** The Q&A screen: asking the knowledge base, and the conversations it leaves. */
    qa: string;
    /** Today's clock: punch in and out. Self-service, so every role has it. */
    clock: string;
    /** The caller's own attendance history and correction requests. */
    attendance: string;
    /** The caller's own leave balances and requests. */
    leave: string;
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
    /** Ticket 34's two states. `knowledge_base_no_basis` is D20's refusal — a normal
     *  answer, not a failure — and `answer_model_unavailable` is the retryable 503 that
     *  replaces a silent fallback to an ungrounded answer. Both are rendered from the
     *  API's `message_key`, so the client never shows the server's Spanish sentence to
     *  an English reader. */
    knowledge_base_no_basis: string;
    answer_model_unavailable: string;
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
    /** Ticket 21's refusals. "Already clocked in" and "there is no open shift" are
     *  states of the clock rather than mistakes, so each has its own sentence saying
     *  what the screen is about to show instead. */
    attendance_already_clocked_in: string;
    attendance_no_open_shift: string;
    attendance_event_in_future: string;
    attendance_employee_terminated: string;
    attendance_correction_not_a_punch: string;
    attendance_range_invalid: string;
    /** Ticket 24's correction document: not there, unusable, or not in a state the
     *  caller may act on. */
    attendance_correction_not_found: string;
    attendance_correction_invalid: string;
    attendance_correction_not_draft: string;
    attendance_correction_target_unresolved: string;
    attendance_correction_submission_refused: string;
    attendance_correction_apply_failed: string;
    /** The kernel's own refusals: this caller may not read or write that record. The
     *  screens show these rather than a role check of their own. */
    forbidden: string;
    not_found: string;
    unauthenticated: string;
    invalid_request: string;
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
  /**
   * The closed set of day states, in words.
   *
   * Top-level rather than inside a screen, because two screens show a day's state — the
   * clock and the attendance record — and the API names its states (`ok`, `absent`) while
   * the interface calls them "shift closed" and "not clocked in yet". One set of words for
   * one closed set of states; a second translation of the same enum would drift.
   */
  dayStatus: {
    label: {
      working: string;
      finished: string;
      notStarted: string;
      missingOut: string;
      incomplete: string;
      holiday: string;
      nonWorking: string;
    };
    /** One sentence saying what the state means for the person reading it. */
    hint: {
      working: string;
      finished: string;
      notStarted: string;
      missingOut: string;
      incomplete: string;
      holiday: string;
      nonWorking: string;
    };
  };
  /**
   * What the nightly pass found wrong with a day, in words.
   *
   * Split out of the clock for the same reason `dayStatus` is: two screens list the same
   * closed set of anomaly types, and a second translation of one enum is a second answer
   * to "what was flagged". `unknown` is the degradation for a type the server has learned
   * and this build has not — a sentence rather than a blank line.
   */
  anomalyType: {
    missing_clock_out: string;
    missing_clock_in: string;
    late: string;
    early_leave: string;
    no_punches: string;
    unknown: string;
  };
  clock: {
    title: string;
    intro: string;
    /** The business day the screen is about, with the date interpolated. */
    businessDate: string;
    /** The single primary action, which changes with the state (§4.2, §6.2). */
    action: {
      clockIn: string;
      clockOut: string;
      clockingIn: string;
      clockingOut: string;
    };
    /** Closing a day is the one thing here that cannot be undone in place. */
    confirm: {
      title: string;
      body: string;
      cancel: string;
      confirm: string;
      close: string;
    };
    /** The feedback §6.2 asks for: the state changed, and a short notice says so. */
    confirmedIn: string;
    confirmedOut: string;
    /** The running timer, and the finished figure beside it. */
    elapsed: string;
    worked: string;
    expected: string;
    /** A holiday or a non-working day: nothing was expected, so nothing is missing. */
    restDay: string;
    /** No schedule reaches this person: the absence of a rule, not a zero. */
    expectedUnknown: string;
    noSchedule: {
      title: string;
      body: string;
    };
    /** A second clock-in while one is open: a stale tab, not a failure. */
    alreadyClockedIn: {
      title: string;
      body: string;
    };
    /** Today's rows. */
    events: {
      heading: string;
      empty: string;
      emptyHint: string;
      time: string;
      kind: string;
      source: string;
      corrected: string;
      madeUp: string;
      original: string;
      chainHeading: string;
    };
    kind: {
      clock_in: string;
      clock_out: string;
      correction: string;
    };
    source: {
      web: string;
      correction: string;
    };
    /** The pattern behind the day, and why it is that one. */
    schedule: {
      heading: string;
      patternLabel: string;
      patternNone: string;
      sourceLabel: string;
      source: {
        override: string;
        department: string;
        default: string;
        none: string;
      };
      holidayLabel: string;
      holidayNone: string;
    };
    /** What the nightly pass flagged, resolved or not. */
    anomalies: {
      heading: string;
      none: string;
      resolved: string;
    };
    loading: string;
    error: string;
    /** What the reader can do about a failed read, beside the retry button. */
    errorHint: string;
    retry: string;
  };
  attendance: {
    title: string;
    intro: string;
    /** The month being shown, with the month interpolated. */
    monthOf: string;
    /** Accessible name of the month navigation landmark. */
    monthNavLabel: string;
    previousMonth: string;
    nextMonth: string;
    thisMonth: string;
    /** The month's totals: worked, expected, and how many days were flagged. */
    summary: {
      heading: string;
      worked: string;
      expected: string;
      /** Expected hours the API could not compute: no stored snapshot and no pattern. */
      expectedUnknown: string;
      /** The figure is a frozen snapshot: the rules that produced it are stored with it. */
      expectedSnapshot: string;
      /** The figure is live: nobody has frozen it, so a schedule edited since counts. */
      expectedLive: string;
      flaggedLabel: string;
      /** The count is a number in the value cell, so the sentence carries no inflection. */
      flaggedNone: string;
    };
    month: {
      /** The section heading above the table; `caption` is the table's own caption. */
      heading: string;
      caption: string;
      date: string;
      status: string;
      firstIn: string;
      lastOut: string;
      worked: string;
      expected: string;
      /** Accessible name of the control that opens one day's record. */
      open: string;
      empty: string;
      emptyHint: string;
      /** Shown in a cell where the API answered with nothing. */
      dash: string;
      /** A day that carries more anomalies than the row can show. */
      flagged: string;
    };
    day: {
      heading: string;
      selectPrompt: string;
      worked: string;
      expected: string;
      /** Expected hours the API answered with nothing for: no pattern reaches the day. */
      expectedUnknown: string;
      overtime: string;
      firstIn: string;
      lastOut: string;
      /** The chain of a punch: what it was written as, and what restated it. */
      chainHeading: string;
      original: string;
      correction: string;
      effective: string;
      supersedes: string;
      reason: string;
      madeUp: string;
      corrected: string;
      noPunches: string;
      anomaliesHeading: string;
      anomaliesNone: string;
      resolved: string;
      unresolved: string;
      detectedAt: string;
    };
    /** The correction document: how to file one, and what it becomes. */
    correction: {
      heading: string;
      description: string;
      dateLabel: string;
      /** The bound the API enforces: only days that have already happened. */
      dateHint: string;
      kindLabel: string;
      timeLabel: string;
      timeHint: string;
      reasonLabel: string;
      reasonPlaceholder: string;
      reasonHint: string;
      submit: string;
      submitting: string;
      filed: string;
      dateRequired: string;
      timeRequired: string;
      reasonRequired: string;
      future: string;
      /** What the two approvals mean for the punch itself. */
      note: string;
      /** Heading of a refusal: the record loaded, the request is what did not go through. */
      refused: string;
      listHeading: string;
      listEmpty: string;
      listEmptyHint: string;
      submittedAt: string;
      appliedAt: string;
      appliedEvent: string;
      viewDay: string;
      /** The state of a document, as words. */
      state: {
        draft: string;
        in_approval: string;
        approved: string;
        applied: string;
        rejected: string;
        withdrawn: string;
      };
      /** Which punch the document is about. */
      kind: {
        clock_in: string;
        clock_out: string;
      };
    };
    export: {
      label: string;
      hint: string;
    };
    loading: string;
    error: string;
    errorHint: string;
    retry: string;
  };
  leave: {
    title: string;
    intro: string;
    /** The year the balances are about, with the year interpolated. */
    yearOf: string;
    /**
     * The configured allowance, with the days interpolated.
     *
     * Phrased so the sentence carries no plural agreement: the count sits at the end, where
     * "1 días" and "1 days" cannot happen — a rule this screen learned the hard way.
     */
    allowance: string;
    balances: {
      heading: string;
      /** One balance's figures. */
      entitled: string;
      carried: string;
      used: string;
      pending: string;
      remaining: string;
      /** A year nobody has needed yet: the figures are what the allowance would grant. */
      projected: string;
      historyHeading: string;
      historyEmpty: string;
      /** The four figures each movement left behind, with both numbers interpolated. */
      historyRow: string;
      empty: string;
      emptyHint: string;
      noAllowanceType: string;
      /** The types that have no annual allowance to track, named once. */
      otherTypes: string;
    };
    /** Where a balance's days came from, or went. */
    entry: {
      grant: string;
      carry_over: string;
      adjustment: string;
      reserve: string;
      release: string;
      consume: string;
      refund: string;
    };
    type: {
      paid: string;
      unpaid: string;
      needsAttachment: string;
      countsAgainstAnnual: string;
      noAttachment: string;
    };
    request: {
      heading: string;
      description: string;
      typeLabel: string;
      typePlaceholder: string;
      startLabel: string;
      endLabel: string;
      attachmentLabel: string;
      attachmentHint: string;
      attachmentRequired: string;
      startRequired: string;
      endRequired: string;
      endBeforeStart: string;
      typeRequired: string;
      /** The step that asks the API to compute the range, before anything is filed. */
      preview: string;
      previewing: string;
      /** What the API answered the range is worth, with the count interpolated. */
      computed: string;
      computedSplit: string;
      /** The draft is written but not filed: the days are not reserved yet. */
      drafted: string;
      submit: string;
      submitting: string;
      discard: string;
      filed: string;
      filedHint: string;
      /** Why there is no note field, said on screen rather than left to be noticed. */
      noNote: string;
      overlapHint: string;
      /** Heading of a refusal: the page loaded, the request is what did not go through. */
      refused: string;
    };
    list: {
      heading: string;
      empty: string;
      emptyHint: string;
      /** How many working days the API charged the range. Count last, so it cannot inflect. */
      days: string;
      daysSplit: string;
      period: string;
      requestedOn: string;
      attachment: string;
      attachmentHrOnly: string;
      withdraw: string;
      withdrawing: string;
      withdrawn: string;
      /** The engine's decisions, one line each. */
      decisionsHeading: string;
      decisionsEmpty: string;
      decisionLine: string;
      level: string;
      round: string;
      comment: string;
      /** The state of a request, as words. */
      state: {
        draft: string;
        in_approval: string;
        approved: string;
        rejected: string;
        withdrawn: string;
      };
      decisions: {
        approved: string;
        rejected: string;
        returned: string;
        pending: string;
        skipped: string;
      };
    };
    calendar: {
      heading: string;
      description: string;
      caption: string;
      previousMonth: string;
      nextMonth: string;
      thisMonth: string;
      /** The month being shown, with the month interpolated. */
      monthOf: string;
      monthNavLabel: string;
      /** Accessible name of one day cell; the date is interpolated. */
      dayLabel: string;
      /** A day covered by approved leave. */
      onLeave: string;
      empty: string;
      emptyHint: string;
      /** The month's approved days, grouped by leave type. */
      summary: string;
      summaryEmpty: string;
      loading: string;
      error: string;
    };
    loading: string;
    error: string;
    errorHint: string;
    retry: string;
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
  /**
   * The Q&A screen (ticket 37).
   *
   * Its own section rather than a few keys under `documents`: it is the interface to §5.2's
   * pipeline — the streamed answer, D20's refusal, the citation panel and the 90-day
   * retention — and a reader looking for the sentence that a refusal shows should find it
   * beside the refusal's own copy rather than in a document list's vocabulary.
   *
   * **The refusal is here only partly.** Its *sentence* is `errors.knowledge_base_no_basis`
   * — the catalogue key the API's `refusal` frame names, so one key serves the stream and
   * every other surface that ever reports it — and what is here is the block around it:
   * the heading and the next steps §4.4 asks for.
   */
  qa: {
    title: string;
    intro: string;
    loading: string;
    conversations: {
      heading: string;
      newConversation: string;
      /** §5.1's retention notice: the number of days is interpolated. */
      retention: string;
      empty: string;
      emptyHint: string;
      error: string;
      retry: string;
      /** How many of the caller's conversations are on screen; both numbers interpolated. */
      count: string;
      /** A row's second line, with the date interpolated. */
      lastMessage: string;
      rename: string;
      renameLabel: string;
      /** Accessible name of one row's rename button, with the title interpolated. */
      renameLabelFor: string;
      titleRequired: string;
      renameFailed: string;
      save: string;
      cancel: string;
      delete: string;
      /** Accessible name of one row's delete button, with the title interpolated. */
      deleteLabelFor: string;
    };
    deleteDialog: {
      title: string;
      /** With the conversation's title interpolated. */
      body: string;
      /** What "deleted" means until the retention sweep removes the row. */
      hint: string;
      confirm: string;
      cancel: string;
      deleting: string;
      failed: string;
    };
    thread: {
      newConversation: string;
      /** An open conversation's retention deadline, with the date interpolated. */
      expiresAt: string;
      empty: string;
      loading: string;
      error: string;
      retry: string;
    };
    answer: {
      /** Before the first token: retrieval is still running. */
      searching: string;
      /** While text is arriving. */
      streaming: string;
    };
    refusal: {
      title: string;
      /** The next steps §4.4 requires, in order. */
      steps: readonly string[];
    };
    failure: {
      title: string;
      retry: string;
    };
    /** The scope banner's fallback, when the payload carries no sentence in this language. */
    scope: {
      fallback: string;
    };
    citation: {
      /** Accessible name of a citation badge, with its number interpolated. */
      open: string;
      listLabel: string;
      /** With the page number interpolated, for a format that has pages. */
      page: string;
      personal: string;
    };
    panel: {
      /** Heading, with the citation's number interpolated. */
      title: string;
      close: string;
      file: string;
      pageLabel: string;
      /** One page, or a range: both interpolate page numbers. */
      page: string;
      pageRange: string;
      section: string;
      provenance: string;
      companyKb: string;
      personalDocument: string;
      scope: string;
      scopeParent: string;
      scopeChild: string;
      quote: string;
      openOriginal: string;
    };
    composer: {
      label: string;
      hint: string;
      submit: string;
      asking: string;
      /** Beside the button while a turn is still arriving. */
      streaming: string;
    };
    /**
     * The draft the assistant prepared (ticket 40).
     *
     * The form's *content* — its title and every field label — comes from the API with the
     * form, because the set of fields is the server's. What is typed here is the screen's
     * own state around it: when the draft lapses, what an expired one means, and what the
     * disabled confirmation is waiting for.
     */
    draft: {
      proposedBadge: string;
      expiredBadge: string;
      expiresAt: string;
      expiredAt: string;
      expiredTitle: string;
      expiredBody: string;
      expiredAction: string;
      confirm: string;
      confirmNote: string;
      factWorkingDays: string;
      factWeek: string;
      factProject: string;
      factTask: string;
      factBillable: string;
      factDay: string;
      factAttachment: string;
      yes: string;
      no: string;
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
