// Only an IPC adapter. The official SDK implements MCP, and the official server owns storage.
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const memoryFile = process.argv[2];
if (!memoryFile || !path.isAbsolute(memoryFile)) process.exit(2);
const client = new Client({ name: 'wechat-didi-memory', version: '0.2.0' });
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [path.join(root, 'node_modules', '@modelcontextprotocol', 'server-memory', 'dist', 'index.js')],
  env: { MEMORY_FILE_PATH: memoryFile },
  stderr: 'ignore',
});

try {
  await client.connect(transport);
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of input) {
    let request;
    try {
      request = JSON.parse(line);
      let result;
      if (request.method === 'list_tools') result = await client.listTools();
      else if (request.method === 'call') result = await client.callTool(request.params);
      else if (request.method === 'close') {
        process.stdout.write(JSON.stringify({ id: request.id, result: {} }) + '\n');
        break;
      } else throw new Error('unsupported IPC method');
      process.stdout.write(JSON.stringify({ id: request.id, result }) + '\n');
    } catch {
      // Never forward raw SDK errors or stored personal data in diagnostic messages.
      process.stdout.write(JSON.stringify({ id: request?.id, error: 'Memory MCP request failed' }) + '\n');
    }
  }
} catch {
  process.exitCode = 1;
} finally {
  await client.close().catch(() => {});
}
