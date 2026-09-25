# syntax=docker/dockerfile:1

# --- dev: bind-mounted source, hot reload ---
FROM node:22-alpine AS dev

WORKDIR /app

# Dependency layer first so source edits do not reinstall packages.
COPY package.json ./
RUN npm install

COPY . .

EXPOSE 3000
CMD ["npm", "run", "dev"]

# --- prod: standalone server build ---
FROM node:22-alpine AS prod

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .
RUN npm run build

EXPOSE 3000
CMD ["npm", "run", "start"]
