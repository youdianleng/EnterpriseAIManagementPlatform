"use client";

import { useState } from "react";

import type { Dictionary } from "@/lib/i18n/dictionaries";
import { Alert } from "@/lib/ui/alert";
import { Button } from "@/lib/ui/button";

/**
 * Standing in for a page when the session check itself failed.
 *
 * It must not look like a signed-out state: the visitor is very likely still
 * signed in and the platform is simply unreachable. Retrying re-runs the Server
 * Component, which is the only thing that can answer the question again.
 */
export function SessionUnavailable({
  dict,
  locale,
}: {
  dict: Dictionary["auth"]["shell"];
  locale: string;
}) {
  const [retrying, setRetrying] = useState(false);

  return (
    <div className="flex flex-1 flex-col items-center justify-center">
      <Alert
        tone="danger"
        role="alert"
        title={dict.sessionUnavailable}
        className="max-w-xl"
      >
        <div className="mt-4">
          <Button
            variant="secondary"
            size="sm"
            aria-busy={retrying || undefined}
            onClick={() => {
              setRetrying(true);
              window.location.assign(`/${locale}`);
            }}
          >
            {dict.retry}
          </Button>
        </div>
      </Alert>
    </div>
  );
}
