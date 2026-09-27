import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // "standalone" makes `next build` also emit .next/standalone: a minimal
  // server.js plus only the node_modules files it actually uses. The
  // production image (frontend/Dockerfile) ships that folder instead of the
  // whole node_modules tree, so it is a fraction of the size. `npm run dev`
  // is unaffected; `npm run start` still works but logs a warning that
  // suggests `node .next/standalone/server.js` instead.
  output: "standalone",
};

export default nextConfig;
