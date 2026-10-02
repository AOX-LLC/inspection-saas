// Loaded with `node --import` ahead of the Next.js server. See forwarded-for.mjs.
import http from "node:http";
import { install } from "./forwarded-for.mjs";

install(http);
