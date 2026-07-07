FROM jupyter/base-notebook:python-3.11.6
WORKDIR /home/jovyan/puhti-extension
COPY . .

USER root
RUN chown -R jovyan:users /home/jovyan/puhti-extension
USER jovyan

RUN pip install --upgrade pip \
    && pip install -e .

RUN jupyter server extension enable jupyterlab_examples_server

EXPOSE 8888
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--allow-root", "--LabApp.token=''"]
