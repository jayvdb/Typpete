class Random:
    def __init__(self, x: object = None) -> None:
        ...

    def seed(self, x: object = None) -> None:
        ...

    def random(self) -> float:
        ...

    def choice(self, s):
        return s[0]
