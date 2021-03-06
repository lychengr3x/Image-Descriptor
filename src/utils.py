import os
import numpy as np
import time
import glob
import pickle
import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence
from model import DecoderRNN
from data import CocoDataset, collate_fn
from build_vocab import Vocabulary
from model import ResNet, VGG
import torchvision.transforms as transforms
from PIL import Image
import matplotlib.pyplot as plt
from nltk.translate.bleu_score import sentence_bleu
from nltk.translate.bleu_score import corpus_bleu
from nltk.translate.bleu_score import SmoothingFunction
from string import punctuation


def get_encoder(args):
    if args.encoder == 'resnet':
        encoder = ResNet(args.embed_size, ver=args.encoder_ver,
                         attention_mechanism=args.attention)
    elif args.encoder == 'vgg':
        encoder = VGG(args.embed_size, ver=args.encoder_ver,
                      attention_mechanism=args.attention)
    else:
        raise NameError('Not supported pretrained network')
    return encoder


def plot_loss(model, fig, axes):
    x_axis = range(1, model.epoch+1)
    axes.clear()
    # training loss
    axes.plot(x_axis, [model.history[k][0]['loss'] for k in range(model.epoch)], '-*',
              label="training loss")
    # evaluation loss
    axes.plot(x_axis, [model.history[k][1]['loss'] for k in range(model.epoch)], '-o',
              label="evaluation loss")
    axes.set_xlabel('Epoch')
    axes.set_ylabel('Loss')
    axes.legend(('training loss', 'evaluation loss'))

    plt.tight_layout()
    fig.canvas.draw()


class Args():
    def __init__(self, log_step=10, save_step=1000, embed_size=256, hidden_size=512, crop_size=224,
                 num_layers=1, num_epochs=5, batch_size=128, num_workers=2, learning_rate=0.001,
                 encoder='resnet', encoder_ver=101, mode='train', attention=False, caption=False, model_dir='../models/',
                 checkpoint=None, vocab_path='../data/vocab.pkl', image_path='../png/example.png',
                 plot=False, image_dir='../data/resized2014', validate_when_training=False,
                 caption_path='../data/annotations/captions_train2014.json'):
        '''
        For jupyter notebook
        '''
        self.log_step = log_step
        self.save_step = save_step
        self.embed_size = embed_size
        self.hidden_size = hidden_size
        self.crop_size = crop_size
        self.num_layers = num_layers
        self.num_epochs = num_epochs
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.learning_rate = learning_rate
        self.encoder = encoder
        self.encoder_ver = encoder_ver
        self.mode = mode
        self.attention = attention
        self.caption = caption
        self.model_dir = model_dir
        self.checkpoint = checkpoint
        self.vocab_path = vocab_path
        self.image_path = image_path
        self.plot = plot
        self.image_dir = image_dir
        self.validate_when_training = validate_when_training
        self.caption_path = caption_path


class StatsManager(object):
    '''
    A class meant to track the loss during a neural network learning experiment.

    Though not abstract, this class is meant to be overloaded to compute and
    track statistics relevant for a given task. For instance, you may want to
    overload its methods to keep track of the accuracy, top-5 accuracy,
    intersection over union, PSNR, etc, when training a classifier, an object
    detector, a denoiser, etc.

    This class is one of course material of UCSD ECE 285 MLIP
    '''

    def __init__(self):
        self.init()

    def __repr__(self):
        """Pretty printer showing the class name of the stats manager. This is
        what is displayed when doing ``print(stats_manager)``.
        """
        return self.__class__.__name__

    def init(self):
        """Initialize/Reset all the statistics"""
        self.running_loss = 0
        self.number_update = 0

    def accumulate(self, loss, x=None, y=None, d=None):
        """Accumulate statistics

        Though the arguments x, y, d are not used in this implementation, they
        are meant to be used by any subclasses. For instance they can be used
        to compute and track top-5 accuracy when training a classifier.

        Arguments:
            loss (float): the loss obtained during the last update.
            x (Tensor): the input of the network during the last update.
            y (Tensor): the prediction of by the network during the last update.
            d (Tensor): the desired output for the last update.
        """
        self.running_loss += loss
        self.number_update += 1

    def summarize(self):
        """Compute statistics based on accumulated ones"""
        return self.running_loss / self.number_update


class ImageDescriptorStatsManager(StatsManager):
    def __init__(self):
        super(ImageDescriptorStatsManager, self).__init__()

    def init(self):
        """Initialize/Reset all the statistics"""
        super(ImageDescriptorStatsManager, self).init()
        self.running_perplexity = 0

    def accumulate(self, loss, perplexity, x=None, y=None, d=None):
        """Accumulate statistics

        Though the arguments x, y, d are not used in this implementation, they
        are meant to be used by any subclasses. For instance they can be used
        to compute and track top-5 accuracy when training a classifier.

        Arguments:
            loss (float): the loss obtained during the last update.
            x (Tensor): the input of the network during the last update.
            y (Tensor): the prediction of by the network during the last update.
            d (Tensor): the desired output for the last update.
        """
        # compute loss from StatsManager
        super(ImageDescriptorStatsManager, self).accumulate(loss)
        # compute perplexity here
        self.running_perplexity += perplexity

    def summarize(self):
        loss = super(ImageDescriptorStatsManager, self).summarize()
        perplexity = self.running_perplexity / self.number_update
        return {'loss': loss, 'perplexity': perplexity}


class ImageDescriptor():
    def __init__(self, args, encoder):
        assert(args.mode == 'train' or 'val' or 'test')
        self.__args = args
        self.__mode = args.mode
        self.__attention_mechanism = args.attention
        self.__stats_manager = ImageDescriptorStatsManager()
        self.__validate_when_training = args.validate_when_training
        self.__history = []

        if not os.path.exists(args.model_dir):
            os.makedirs(args.model_dir)

        self.__config_path = os.path.join(
            args.model_dir, f'config-{args.encoder}{args.encoder_ver}.txt')

        # Device configuration
        self.__device = torch.device(
            'cuda' if torch.cuda.is_available() else 'cpu')

        # training set vocab
        with open(args.vocab_path, 'rb') as f:
            self.__vocab = pickle.load(f)

        # validation set vocab
        with open(args.vocab_path.replace('train', 'val'), 'rb') as f:
            self.__vocab_val = pickle.load(f)

        # coco dataset
        self.__coco_train = CocoDataset(
            args.image_dir, args.caption_path, self.__vocab, args.crop_size)
        self.__coco_val = CocoDataset(
            args.image_dir, args.caption_path.replace('train', 'val'), self.__vocab_val, args.crop_size)

        # data loader
        self.__train_loader = torch.utils.data.DataLoader(dataset=self.__coco_train,
                                                          batch_size=args.batch_size,
                                                          shuffle=True,
                                                          num_workers=args.num_workers,
                                                          collate_fn=collate_fn)
        self.__val_loader = torch.utils.data.DataLoader(dataset=self.__coco_val,
                                                        batch_size=args.batch_size,
                                                        shuffle=False,
                                                        num_workers=args.num_workers,
                                                        collate_fn=collate_fn)
        # Build the models
        self.__encoder = encoder.to(self.__device)
        self.__decoder = DecoderRNN(args.embed_size, args.hidden_size,
                                    len(self.__vocab), args.num_layers, attention_mechanism=self.__attention_mechanism).to(self.__device)

        # Loss and optimizer
        self.__criterion = nn.CrossEntropyLoss()
        self.__params = list(self.__decoder.parameters(
        )) + list(self.__encoder.linear.parameters()) + list(self.__encoder.bn.parameters())
        self.__optimizer = torch.optim.Adam(
            self.__params, lr=args.learning_rate)

        # Load checkpoint and check compatibility
        if os.path.isfile(self.__config_path):
            with open(self.__config_path, 'r') as f:
                content = f.read()[:-1]
            if content != repr(self):
                # save the error info
                with open('config.err', 'w') as f:
                    print(f'f.read():\n{content}', file=f)
                    print(f'repr(self):\n{repr(self)}', file=f)
                raise ValueError(
                    "Cannot create this experiment: "
                    "I found a checkpoint conflicting with the current setting.")
            self.load(file_name=args.checkpoint)
        else:
            self.save()

    def setting(self):
        '''
        Return the setting of the experiment.
        '''
        return {'Net': (self.__encoder, self.__decoder),
                'Optimizer': self.__optimizer,
                'BatchSize': self.__args.batch_size}

    @property
    def epoch(self):
        return len(self.__history)

    @property
    def history(self):
        return self.__history

    # @property
    # def mode(self):
    #     return self.__args.mode

    # @mode.setter
    # def mode(self, m):
    #     self.__args.mode = m

    def __repr__(self):
        '''
        Pretty printer showing the setting of the experiment. This is what
        is displayed when doing `print(experiment). This is also what is
        saved in the `config.txt file.
        '''
        string = ''
        for key, val in self.setting().items():
            string += '{}({})\n'.format(key, val)
        return string

    def state_dict(self):
        '''
        Returns the current state of the model.
        '''
        return {'Net': (self.__encoder.state_dict(), self.__decoder.state_dict()),
                'Optimizer': self.__optimizer.state_dict(),
                'History': self.__history}

    def save(self):
        '''
        Saves the model on disk, i.e, create/update the last checkpoint.
        '''
        file_name = os.path.join(
            self.__args.model_dir, '{}{}-epoch-{}.ckpt'.format(self.__args.encoder, self.__args.encoder_ver, self.epoch))
        torch.save(self.state_dict(), file_name)
        with open(self.__config_path, 'w') as f:
            print(self, file=f)

        print(f'Save to {file_name}.')

    def load(self, file_name=None):
        '''
        Loads the model from the last checkpoint saved on disk.

        Args:
            file_name (str): path to the checkpoint file
        '''
        if not file_name:
            # find the latest .ckpt file
            try:
                file_name = max(
                    glob.iglob(os.path.join(self.__args.model_dir, '*.ckpt')), key=os.path.getctime)
                print(f'Load from {file_name}.')
            except:
                raise FileNotFoundError(
                    'No checkpoint file in the model directory.')
        else:
            file_name = os.path.join(self.__args.model_dir, file_name)
            print(f'Load from {file_name}.')

        try:
            checkpoint = torch.load(file_name, map_location=self.__device)
        except:
            raise FileNotFoundError(
                'Please check --checkpoint, the name of the file')

        self.load_state_dict(checkpoint)
        del checkpoint

    def load_state_dict(self, checkpoint):
        '''
        Loads the model from the input checkpoint.

        Args:
            checkpoint: an object saved with torch.save() from a file.
        '''
        self.__encoder.load_state_dict(checkpoint['Net'][0])
        self.__decoder.load_state_dict(checkpoint['Net'][1])
        self.__optimizer.load_state_dict(checkpoint['Optimizer'])
        self.__history = checkpoint['History']

        # The following loops are used to fix a bug that was
        # discussed here: https://github.com/pytorch/pytorch/issues/2830
        # (it is supposed to be fixed in recent PyTorch version)
        for state in self.__optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.to(self.__device)

    def train(self, plot_loss=None):
        '''
        Train the network using backpropagation based
        on the optimizer and the training set.

        Args:
            plot_loss (func, optional): if not None, should be a function taking a
                single argument being an experiment (meant to be `self`).
                Similar to a visitor pattern, this function is meant to inspect
                the current state of the experiment and display/plot/save
                statistics. For example, if the experiment is run from a
                Jupyter notebook, `plot` can be used to display the evolution
                of the loss with `matplotlib`. If the experiment is run on a
                server without display, `plot` can be used to show statistics
                on `stdout` or save statistics in a log file. (default: None)
        '''
        self.__encoder.train()
        self.__decoder.train()
        self.__stats_manager.init()
        total_step = len(self.__train_loader)
        start_epoch = self.epoch
        print("Start/Continue training from epoch {}".format(start_epoch))

        if plot_loss is not None:
            plot_loss(self)

        for epoch in range(start_epoch, self.__args.num_epochs):
            t_start = time.time()
            self.__stats_manager.init()
            for i, (images, captions, lengths) in enumerate(self.__train_loader):
                # Set mini-batch dataset
                if not self.__attention_mechanism:
                    images = images.to(self.__device)
                    captions = captions.to(self.__device)
                else:
                    with torch.no_grad():
                        images = images.to(self.__device)
                    captions = captions.to(self.__device)

                targets = pack_padded_sequence(
                    captions, lengths, batch_first=True)[0]

                # Forward, backward and optimize
                if not self.__attention_mechanism:
                    features = self.__encoder(images)
                    outputs = self.__decoder(features, captions, lengths)
                    self.__decoder.zero_grad()
                    self.__encoder.zero_grad()
                else:
                    self.__encoder.zero_grad()
                    self.__decoder.zero_grad()
                    features, cnn_features = self.__encoder(images)
                    outputs = self.__decoder(
                        features, captions, lengths, cnn_features=cnn_features)
                loss = self.__criterion(outputs, targets)

                loss.backward()
                self.__optimizer.step()
                with torch.no_grad():
                    self.__stats_manager.accumulate(
                        loss=loss.item(), perplexity=np.exp(loss.item()))

                # Print log info each iteration
                if i % self.__args.log_step == 0:
                    print('[Training] Epoch: {}/{} | Step: {}/{} | Loss: {:.4f} | Perplexity: {:5.4f}'
                          .format(epoch+1, self.__args.num_epochs, i, total_step, loss.item(), np.exp(loss.item())))

            if not self.__validate_when_training:
                self.__history.append(self.__stats_manager.summarize())
                print("Epoch {} | Time: {:.2f}s\nTraining Loss: {:.6f} | Training Perplexity: {:.6f}".format(
                    self.epoch, time.time() - t_start, self.__history[-1]['loss'], self.__history[-1]['perplexity']))
            else:
                self.__history.append(
                    (self.__stats_manager.summarize(), self.evaluate()))
                print("Epoch {} | Time: {:.2f}s\nTraining Loss: {:.6f} | Training Perplexity: {:.6f}\nEvaluation Loss: {:.6f} | Evaluation Perplexity: {:.6f}".format(
                    self.epoch, time.time() - t_start,
                    self.__history[-1][0]['loss'], self.__history[-1][0]['perplexity'],
                    self.__history[-1][1]['loss'], self.__history[-1][1]['perplexity']))

            # Save the model checkpoints
            self.save()

            if plot_loss is not None:
                plot_loss(self)

        print("Finish training for {} epochs".format(self.__args.num_epochs))

    def evaluate(self, print_info=False):
        '''
        Evaluates the experiment, i.e., forward propagates the validation set
        through the network and returns the statistics computed by the stats
        manager.

        Args:
            print_info (bool): print the results of loss and perplexity
        '''
        self.__stats_manager.init()
        self.__encoder.eval()
        self.__decoder.eval()
        total_step = len(self.__val_loader)
        with torch.no_grad():
            for i, (images, captions, lengths) in enumerate(self.__val_loader):
                images = images.to(self.__device)
                captions = captions.to(self.__device)
                targets = pack_padded_sequence(
                    captions, lengths, batch_first=True)[0]

                # Forward
                if not self.__attention_mechanism:
                    features = self.__encoder(images)
                    outputs = self.__decoder(features, captions, lengths)
                else:
                    features, cnn_features = self.__encoder(images)
                    outputs = self.__decoder(
                        features, captions, lengths, cnn_features=cnn_features)
                loss = self.__criterion(outputs, targets)
                self.__stats_manager.accumulate(
                    loss=loss.item(), perplexity=np.exp(loss.item()))
                if i % self.__args.log_step == 0:
                    print('[Validation] Step: {}/{} | Loss: {:.4f} | Perplexity: {:5.4f}'
                          .format(i, total_step, loss.item(), np.exp(loss.item())))

        summarize = self.__stats_manager.summarize()
        if print_info:
            print(
                f'[Validation] Average loss for this epoch is {summarize["loss"]:.6f}')
            print(
                f'[Validation] Average perplexity for this epoch is {summarize["perplexity"]:.6f}\n')
        self.__encoder.train()
        self.__decoder.train()
        return summarize

    def mode(self, mode=None):
        '''
        Get the current mode or change mode.

        Args:
            mode (str): 'train' or 'eval' mode
        '''
        if not mode:
            return self.__mode
        self.__mode = mode

    def __load_image(self, image):
        '''
        Load image at `image_path` for evaluation.

        Args:
            image (PIL Image): image
        '''
        image = image.resize([224, 224], Image.LANCZOS)

        transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.485, 0.456, 0.406),
                                 (0.229, 0.224, 0.225))])
        image = transform(image).unsqueeze(0)

        return image

    def test(self, image_path=None, plot=False):
        '''
        Evaluate the model by generating the caption for the
        corresponding image at `image_path`.

        Note: This function will not provide BLEU socre.

        Args:
            image_path (str): file path of the evaluation image
            plot (bool): plot or not
        '''
        self.__encoder.eval()
        self.__decoder.eval()

        with torch.no_grad():
            if not image_path:
                image_path = self.__args.image_path

            image = Image.open(image_path)

            # only process with RGB image
            if np.array(image).ndim == 3:
                img = self.__load_image(image).to(self.__device)

                # generate an caption
                if not self.__attention_mechanism:
                    feature = self.__encoder(img)
                    sampled_ids = self.__decoder.sample(feature)
                    sampled_ids = sampled_ids[0].cpu().numpy()
                else:
                    feature, cnn_features = self.__encoder(img)
                    sampled_ids = self.__decoder.sample(feature, cnn_features)
                    sampled_ids = sampled_ids.cpu().data.numpy()

                # Convert word_ids to words
                sampled_caption = []
                for word_id in sampled_ids:
                    word = self.__vocab.idx2word[word_id]
                    sampled_caption.append(word)
                    if word == '<end>':
                        break
                sentence = ' '.join(sampled_caption[1:-1])

                # Print out the image and the generated caption
                print(sentence)

                if plot:
                    image = Image.open(image_path)
                    plt.imshow(np.asarray(image))
            else:
                print('Not support for non-RGB image.')
        self.__encoder.train()
        self.__decoder.train()

    def coco_image(self, idx, ds='val'):
        '''
        Access iamge_id (which is part of the file name) 
        and corresponding image caption of index `idx` in COCO dataset.

        Note: For jupyter notebook

        Args:
            idx (int): index of COCO dataset

        Returns:
            (dict)
        '''
        assert(ds == 'train' or 'val')

        if ds == 'train':
            ann_id = self.__coco_train.ids[idx]
            return self.__coco_train.coco.anns[ann_id]
        else:
            ann_id = self.__coco_val.ids[idx]
            return self.__coco_val.coco.anns[ann_id]

    @property
    def len_of_train_set(self):
        '''
        Number of training 
        '''
        return len(self.__coco_train)

    @property
    def len_of_val_set(self):
        return len(self.__coco_val)

    def bleu_score(self, idx, ds='val', plot=False, show_caption=False):
        '''
        Evaluate the BLEU score for index `idx` in COCO dataset.

        Note: For jupyter notebook

        Args:
            idx (int): index
            ds (str): training or validation dataset
            plot (bool): plot the image or not

        Returns:
            score (float): bleu score
        '''
        assert(ds == 'train' or 'val')
        self.__encoder.eval()
        self.__decoder.eval()

        with torch.no_grad():
            try:
                if ds == 'train':
                    ann_id = self.__coco_train.ids[idx]
                    coco_ann = self.__coco_train.coco.anns[ann_id]
                else:
                    ann_id = self.__coco_val.ids[idx]
                    coco_ann = self.__coco_val.coco.anns[ann_id]
            except:
                raise IndexError('Invalid index')

            image_id = coco_ann['image_id']

            image_id = str(image_id)
            if len(image_id) != 6:
                for _ in range(6 - len(image_id)):
                    image_id = '0' + image_id

            image_path = f'{self.__args.image_dir}/COCO_train2014_000000{image_id}.jpg'
            if ds == 'val':
                image_path = image_path.replace('train', 'val')

            coco_list = coco_ann['caption'].split()

            image = Image.open(image_path)

            if np.array(image).ndim == 3:
                img = self.__load_image(image).to(self.__device)

                # generate an caption
                if not self.__attention_mechanism:
                    feature = self.__encoder(img)
                    sampled_ids = self.__decoder.sample(feature)
                    sampled_ids = sampled_ids[0].cpu().numpy()
                else:
                    feature, cnn_features = self.__encoder(img)
                    sampled_ids = self.__decoder.sample(feature, cnn_features)
                    sampled_ids = sampled_ids.cpu().data.numpy()

                # Convert word_ids to words
                sampled_caption = []
                for word_id in sampled_ids:
                    word = self.__vocab.idx2word[word_id]
                    sampled_caption.append(word)
                    if word == '<end>':
                        break

                # strip punctuations and spacing
                sampled_list = [c for c in sampled_caption[1:-1]
                                if c not in punctuation]

                score = sentence_bleu(coco_list, sampled_list,
                                      smoothing_function=SmoothingFunction().method4)

                if plot:
                    plt.figure()
                    image = Image.open(image_path)
                    plt.imshow(np.asarray(image))
                    plt.title(f'score: {score}')
                    plt.xlabel(f'file: {image_path}')

                # Print out the generated caption
                if show_caption:
                    print(f'Sampled caption:\n{sampled_list}')
                    print(f'COCO caption:\n{coco_list}')

            else:
                print('Not support for non-RGB image.')
                return

        return score
