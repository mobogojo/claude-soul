export type SoulConfig = {
  signals: {
    enabled: boolean;
    maxLogSizeKb: number;
  };
  selfEvaluation: {
    enabled: boolean;
    weight: number;
  };
  stateEngine: {
    enabled: boolean;
  };
  reflection: {
    enabled: boolean;
    quickSignalThreshold: number;
    deepSignalThreshold: number;
    quickIntervalMs: number;
    deepIntervalMs: number;
    quickModel: string;
    deepModel: string;
  };
  exemplars: {
    enabled: boolean;
    maxCount: number;
    maxInjectCount: number;
  };
  lessons: {
    enabled: boolean;
    maxCount: number;
    maxInjectCount: number;
  };
  contextBudget: {
    maxTokens: number;
    /**
     * Hard ceiling on the assembled context in bytes. The token budget is an
     * estimate; this is the size that actually goes on the wire, and an MCP
     * stdio response is a single newline-delimited JSON line. Clients that do
     * not reassemble a message split across pipe reads fail to decode an
     * oversized one, so this is enforced as a backstop after the token pass.
     */
    maxBytes: number;
    /**
     * Cap on how many frameworks each always-included list may render. These
     * lists grow with the framework store, which is what pushes the context
     * past its budget over time. Entries are sorted by confidence, so the cap
     * keeps the strongest.
     */
    maxFrameworkEntries: number;
  };
  tensions: {
    enabled: boolean;
  };
  metaOptimization: {
    enabled: boolean;
  };
  writeProtection: {
    enabled: boolean;
  };
};

export const DEFAULT_CONFIG: SoulConfig = {
  signals: { enabled: true, maxLogSizeKb: 50 },
  selfEvaluation: { enabled: true, weight: 0.5 },
  stateEngine: { enabled: true },
  reflection: {
    enabled: true,
    quickSignalThreshold: 20,
    deepSignalThreshold: 100,
    quickIntervalMs: 1800000,
    deepIntervalMs: 10800000,
    quickModel: "haiku",
    deepModel: "sonnet",
  },
  exemplars: { enabled: true, maxCount: 50, maxInjectCount: 2 },
  lessons: { enabled: true, maxCount: 100, maxInjectCount: 3 },
  contextBudget: { maxTokens: 4500, maxBytes: 16384, maxFrameworkEntries: 40 },
  tensions: { enabled: true },
  metaOptimization: { enabled: true },
  writeProtection: { enabled: true },
};
