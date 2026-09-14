"""slas_station_runner: the daemon on a physical test station (CLAUDE.md §5.2, §10.3).

The platform never receives the station's display; it sends signed step batches over mTLS
and receives results and screenshots. Import from the submodules: `protocol`, `runner`,
`server`, `fakes`.
"""
