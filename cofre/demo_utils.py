from .agent import Cofre
from .policy import RateLimiter


def cofre_com_sms(**kw):
    """Cofre com o 'SMS' do 2FA capturado numa lista (simulação do celular do cliente)."""
    sms = []
    c = Cofre(canal_2fa=lambda conta, cod: sms.append(cod), rate=kw.pop("rate", RateLimiter(max_msgs=10_000)), **kw)
    return c, sms
