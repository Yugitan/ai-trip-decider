import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// 每个用例结束后卸载 DOM：组件里有 useEffect（健康检查等），
// 不清理会让「卸载后 setState」的问题被掩盖。
afterEach(() => {
  cleanup();
});
