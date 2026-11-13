from typing import Optional
from loguru import logger


class PositionSizer:
    """Config holder plus the two policy checks on the auto-copy path.

    Actual sizing lives in ``main.py`` (``copy_existing_position_snapshot`` and
    the fill handler) because it needs live exchange margin, so this class only
    keeps the leverage adjustment and the entry-quality gate, and stores the
    sizing settings that ``/status`` reports.
    """
    
    def __init__(
        self,
        mode: str = "proportional",
        fixed_size: float = 100.0,
        portfolio_ratio: float = 0.01,
        max_position_size: Optional[float] = None,
        max_total_exposure: Optional[float] = None
    ):
        """
        Initialize position sizer
        
        Args:
            mode: "proportional" or "fixed"
            fixed_size: Fixed position size in USD for fixed mode
            portfolio_ratio: Ratio for proportional mode (e.g., 0.01 = 1:100)
            max_position_size: Maximum size for a single position (None = uncapped)
            max_total_exposure: Maximum total exposure across all positions (None = uncapped)
        """
        self.mode = mode
        self.fixed_size = fixed_size
        self.portfolio_ratio = portfolio_ratio
        self.max_position_size = max_position_size
        self.max_total_exposure = max_total_exposure
        
        logger.info(f"Position Sizer initialized - Mode: {mode}, Ratio: {portfolio_ratio}")
    
    def calculate_leverage(
        self,
        target_leverage: float,
        adjustment_ratio: float = 0.5,
        max_leverage: float = 10.0,
        min_leverage: float = 1.0
    ) -> float:
        """
        Calculate adjusted leverage
        
        Args:
            target_leverage: Target wallet's leverage
            adjustment_ratio: Multiply target leverage by this (e.g., 0.5 = half)
            max_leverage: Maximum allowed leverage
            min_leverage: Minimum leverage
            
        Returns:
            Adjusted leverage value
        """
        adjusted = target_leverage * adjustment_ratio
        adjusted = max(min_leverage, min(adjusted, max_leverage))
        
        logger.debug(f"Leverage adjustment: {target_leverage}x -> {adjusted}x (ratio: {adjustment_ratio})")
        
        return adjusted
    
    def should_copy_position(
        self,
        target_entry_price: float,
        current_market_price: float,
        max_entry_deviation_pct: float = 5.0
    ) -> bool:
        """
        Determine if position should be copied based on entry quality
        
        Args:
            target_entry_price: Price target entered at
            current_market_price: Current market price
            max_entry_deviation_pct: Maximum acceptable price deviation %
            
        Returns:
            True if should copy, False if price has moved too much
        """
        if target_entry_price <= 0:
            return False
        
        deviation_pct = abs(current_market_price - target_entry_price) / target_entry_price * 100
        
        if deviation_pct > max_entry_deviation_pct:
            logger.warning(
                f"Entry quality check failed: Price moved {deviation_pct:.2f}% "
                f"(max: {max_entry_deviation_pct:.2f}%)"
            )
            return False
        
        logger.info(f"Entry quality OK: {deviation_pct:.2f}% deviation")
        return True
