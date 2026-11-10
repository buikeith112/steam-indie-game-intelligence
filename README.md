# steam-indie-game-intelligence

Possible Business Questions:
- Does early review sentiment relate to later popularity in indie games?
- Which genres have the highest proportion of positively reviewed games in the indie market
- What range of pricing is best for popular indie games?
- Which genres of indie games have higher total player counts and median playtimes, which could be correlated to player retention and high replayability?

Breakdown of project:
- Analyze existing Steam Store APIs on game catalog, game metadata, player reviews and ratings, and Steam Store public information
- Retrieve information from APIs accounting for possible challenges such as retrieving data from multiple API endpoints, missing or inconsistent data, real-time updates from games that are added or removed on the store, changing prices, newly written reviews, etc.
- Create a custom database with tables containing information such as `games`, `publishers`, `genres`, `game_genres`, `reviews`, `price_history`, and possible others.
- Create a Power BI dashboard that uses the custom database to answer our previously stated business questions.