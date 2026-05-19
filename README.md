# NBA Data Archive

Mirror of NBA play-by-play, shot detail, and matchup data from 1996/97 through 2025/26.

Built and maintained for [HoopsMatic.com](https://hoopsmatic.com) analytics tools and [HoopsHype](https://hoopshype.com) editorial automation.

## Data sources

- **NBA stats:** Original collection by Vladislav Shufinskiy ([github.com/shufinskiy/nba_data](https://github.com/shufinskiy/nba_data)), licensed under Apache 2.0. Raw archives mirrored here.
- **Underlying APIs:** stats.nba.com, data.nba.com, cdn.nba.com, pbpstats.com.

## Data types

- `shotdetail` - Every shot 1996/97-present with X/Y court coordinates
- `matchups` - Player-vs-player matchup data 2017/18-present
- `nbastats` - Classic play-by-play from stats.nba.com
- `nbastatsv3` - Newer play-by-play schema
- `pbpstats` - Possession-level data with start-type tags
- `datanba` - Play-by-play with on-court coordinates
- `cdnnba` - Lightweight play-by-play from cdn.nba.com

Both regular season and playoff data included.

## License

This mirror is distributed under the Apache License 2.0, matching the upstream source. See `LICENSE`.

## Attribution

Original data collection by Vladislav Shufinskiy ([@vshufinskiy](https://x.com/vshufinskiy)). This repository redistributes that work under Apache 2.0 with full credit.

Statistics themselves are not copyrightable (Feist Publications v. Rural Telephone Service, 1991). Player names, team names, and game data are factual information used in a nominative reference capacity. No NBA trademarks, logos, or proprietary content are used in this repository or in any tools built from it.
