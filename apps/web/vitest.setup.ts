import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// No vitest globals, so testing-library can't register auto-cleanup itself.
afterEach(() => cleanup());
