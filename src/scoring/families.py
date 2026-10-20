from typing import List, Dict, Any

VALID_FAMILIES = {
    'Trend',
    'Momentum',
    'OrderFlow',
    'Volatility',
    'Structure',
    'Manipulation'
}

def reduce_family_signals(signals: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Orthogonal Family Reduction:
    Groups signals by feature family and takes the strongest signal (highest likelihood ratio)
    within each family to avoid double-counting correlated indicators.
    """
    reduced: Dict[str, Dict[str, Any]] = {}
    for sig in signals:
        fam = sig.get('family', 'Structure')
        lr = sig.get('likelihood_ratio', 1.0)
        if fam not in reduced or lr > reduced[fam].get('likelihood_ratio', 1.0):
            reduced[fam] = sig
    return reduced
