class CustomerManager:
    """
    In-memory customers: {id, name, balance}
    """
    def __init__(self):
        self.customers = []
        self._next_id = 1

    def add_customer(self, name: str, balance: float = 0.0):
        rec = {'id': self._next_id, 'name': name, 'balance': float(balance)}
        self._next_id += 1
        self.customers.append(rec)
        return rec

    def get_customer(self, cid: int):
        for c in self.customers:
            if c['id'] == cid:
                return c
        return None

    def list_all(self):
        return list(self.customers)

    def recharge(self, cid: int, amount: float) -> bool:
        c = self.get_customer(cid)
        if not c:
            return False
        c['balance'] += amount
        return True

    def deduct(self, cid: int, amount: float) -> bool:
        c = self.get_customer(cid)
        if not c:
            return False
        if c['balance'] < amount:
            return False
        c['balance'] -= amount
        return True

    def remove(self, cid: int) -> bool:
        for i, c in enumerate(self.customers):
            if c['id'] == cid:
                del self.customers[i]
                return True
        return False
