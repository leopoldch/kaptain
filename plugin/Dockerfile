# Reproducible build: pinned Go and base image, no network at run time.
# The Kubernetes version this plugin is compiled against is pinned in go.mod (v1.31.0)
# and must match the kind nodes and the scheduler image.
FROM golang:1.23.1-bookworm AS build

WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY cmd ./cmd
COPY pkg ./pkg

ARG TARGETOS=linux
ARG TARGETARCH
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags="-s -w" -o /out/kaptain-scheduler ./cmd/kaptain-scheduler

FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=build /out/kaptain-scheduler /usr/local/bin/kaptain-scheduler

USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/kaptain-scheduler"]
