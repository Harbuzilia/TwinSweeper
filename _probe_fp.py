import flet

print("MRO:", [c.__name__ for c in flet.FilePicker.__mro__])
print("module:", flet.FilePicker.__module__)
print("Page.services attr:", "services" in dir(flet.Page))
import inspect
from flet.controls.services.service import Service
src = inspect.getsource(Service)
print(src[:1500])
