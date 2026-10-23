// Code runner: executes untrusted JavaScript in a sandbox.
// `out` is a fluent output helper; its methods return `this` for chaining.
const http = require("http");
const { VM } = require("vm2");

const MAX_CODE = 65536;
const PORT = 3000;

function makeOutput() {
  const lines = [];
  const push = (value) => lines.push(String(value));
  return {
    tag: function (name) {
      push(`[${name}]`);
      return this;
    },
    line: function (text) {
      push(String(text));
      return this;
    },
    text: function (text) {
      push(String(text));
      return this;
    },
    dump: function () {
      return lines.join("\n");
    },
  };
}

function run(code) {
  if (typeof code !== "string" || !code.trim() || code.length > MAX_CODE) {
    throw new Error("code must be a nonempty string of bounded size");
  }
  const out = makeOutput();
  const vm = new VM({ timeout: 2000, sandbox: { out } });
  const result = vm.run(code);
  return { result: result === undefined ? null : result, output: out.dump() };
}

const server = http.createServer((request, response) => {
  const reply = (status, payload) => {
    const body = JSON.stringify(payload);
    response.writeHead(status, {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(body),
    });
    response.end(body);
  };

  if (request.method === "GET" && request.url === "/") {
    reply(200, {
      service: "code runner",
      endpoint: "/run",
      api: ["out.tag", "out.line", "out.text", "out.dump"],
    });
    return;
  }
  if (request.method !== "POST" || request.url !== "/run") {
    reply(404, { error: "not found" });
    return;
  }

  let raw = "";
  request.setEncoding("utf8");
  request.on("data", (chunk) => {
    raw += chunk;
    if (raw.length > MAX_CODE + 1024) {
      reply(413, { error: "request too large" });
      request.destroy();
    }
  });
  request.on("end", () => {
    if (response.writableEnded) {
      return;
    }
    try {
      const body = JSON.parse(raw);
      reply(200, run(body.code));
    } catch (error) {
      reply(400, { error: String((error && error.message) || error) });
    }
  });
});

server.listen(PORT, "0.0.0.0");
