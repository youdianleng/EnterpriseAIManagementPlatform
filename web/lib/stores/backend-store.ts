"use client";

import { create } from "zustand";

import { ApiError, type AppInfo, fetchAppInfo } from "@/lib/api/client";

export type BackendStatus = "idle" | "loading" | "ready" | "error";

type BackendState = {
  status: BackendStatus;
  info: AppInfo | null;
  errorMessage: string | null;
  load: () => Promise<void>;
};

/**
 * Cross-component state for the backend probe.
 *
 * The fetch lives in the store rather than the component so that every consumer
 * shares one request and one status.
 */
export const useBackendStore = create<BackendState>((set, get) => ({
  status: "idle",
  info: null,
  errorMessage: null,

  load: async () => {
    if (get().status === "loading") return;

    set({ status: "loading", errorMessage: null });
    try {
      const info = await fetchAppInfo();
      set({ status: "ready", info, errorMessage: null });
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "Unknown error";
      set({ status: "error", info: null, errorMessage: message });
    }
  },
}));
