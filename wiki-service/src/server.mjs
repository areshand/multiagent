import { createWikiApp } from "./app.mjs";
import { loadConfig } from "./config.mjs";

const config = loadConfig();
const { server } = await createWikiApp(config);
server.listen(config.port, config.host, () => {
  console.log(`wiki service listening on http://${config.host}:${config.port} with root ${config.root}`);
});

function shutdown(signal) {
  console.log(`received ${signal}; stopping wiki service`);
  server.close((error) => {
    if (error) {
      console.error(error);
      process.exitCode = 1;
    }
  });
}

process.once("SIGINT", () => shutdown("SIGINT"));
process.once("SIGTERM", () => shutdown("SIGTERM"));
