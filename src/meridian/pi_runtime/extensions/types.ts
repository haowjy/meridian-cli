export type ToolDefinition = {
  name: string;
  description: string;
  inputSchema: unknown;
};

export type ToolRegistration = {
  tool: ToolDefinition;
  call: (input: unknown, context: unknown) => Promise<unknown>;
};

export type SessionEvent = {
  type?: string;
  [key: string]: unknown;
};

export type SessionMessageOptions = {
  deliverAs?: "followUp" | string;
  triggerTurn?: boolean;
};

export type SessionBridge = {
  on: (event: string, handler: (event: SessionEvent) => void) => void;
  sendMessage: (message: string, options?: SessionMessageOptions) => Promise<void>;
};

export type ExtensionAPI = {
  registerTool?: (definition: ToolRegistration) => void;
  registerHook?: (name: string, handler: (...args: unknown[]) => unknown) => void;
  registerCommand?: (
    name: string,
    spec: {
      description: string;
      handler: (args: string, ctx: unknown) => Promise<void>;
    },
  ) => void;
  on?: (event: string, handler: (...args: unknown[]) => unknown) => void;
  events?: { emit: (channel: string, payload: Record<string, unknown>) => void };
  session?: SessionBridge;
};
