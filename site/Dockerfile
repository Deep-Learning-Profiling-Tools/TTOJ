FROM node:22-bookworm-slim

ARG TTOJ_SOURCE_REF=main
ENV TTOJ_SOURCE_REF=${TTOJ_SOURCE_REF}

WORKDIR /opt/hydro
RUN npm install --omit=dev --no-audit --no-fund hydrooj@5.0.7 @hydrooj/ui-default@4.58.5

ENV PATH="/opt/hydro/node_modules/.bin:${PATH}" \
    NODE_APP_INSTANCE=0 \
    DEFAULT_STORE_PATH=/data/files

COPY entrypoint.sh bootstrap.cjs healthcheck.cjs /app/
COPY tt-site /opt/hydro/tt-site
RUN chmod +x /app/entrypoint.sh
EXPOSE 8888
ENTRYPOINT ["/app/entrypoint.sh"]
