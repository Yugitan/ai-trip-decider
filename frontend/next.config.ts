import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // 后端地址只从环境变量取，前端不得内联任何密钥
  env: {
    NEXT_PUBLIC_API_BASE_URL: process.env.API_BASE_URL ?? "http://127.0.0.1:8000",
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
