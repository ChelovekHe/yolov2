"""
A Threaded Data Generator for YOLOv2

It will generates two things for every batch:

1) Batch of images (in numpy matrices):
   * Dimension : [bach_size, IMG_INPUT, IMG_INPUT, N_ANCHORS * (N_CLASSES+5)]
   * Contain preprocessed input (normalized, re-sized) images

2) Batch of ground truth labels       :
    * Dimension: [batch_size, (IMG_INPUT / SHRINK_FACTOR), (IMG_INPUT / SHRINK_FACTOR), N_ANCHORS * (N_CLASSES+5)]
    * Each ground truth contain : xc, yc, w, h, to, one_hot_labels

"""
import os
import cv2
import pandas as pd
import threading
from sklearn.utils import shuffle

from utils.box import Box, convert_bbox
from utils.augment_img import random_transform, preprocess_img
from cfg import *


class threadsafe_iter:
    """Takes an iterator/generator and makes it thread-safe by
    serializing call to the `next` method of given iterator/generator.
    """
    def __init__(self, it):
        self.it = it
        self.lock = threading.Lock()

    def __iter__(self):
        return self

    def next(self):
        with self.lock:
            return self.it.next()


def threadsafe_generator(f):
    """A decorator that takes a generator function and makes it thread-safe.
    """
    def g(*a, **kw):
        return threadsafe_iter(f(*a, **kw))
    return g


@threadsafe_generator
def flow_from_list(x, y, batch_size=32, scaling_factor=5, augment_data=True):
    """
    An ImageGenerator for Densely YOLO

    Parameters
    ---------
    :param x: list of image paths 
    :param y: list of labels as [Box, label_name]

    :param scaling_factor: the level of augmentation. The higher, the more data being augmented
    :param batch_size:     number of images yielded every iteration
    :param augment_data:   enable data augmentation

    Return
    ------
        generate (images, labels) in batch_size
    """
    # @TODO: thread-safe generator (to allow nb_workers > 1)
    slices = int(len(x) / batch_size)
    if augment_data is True:
        augment_level = calc_augment_level(y, scaling_factor)  # (less data / class means more augmentation)

        # Get list of classes
    fl = open(CATEGORIES, 'r')
    CLASSES = np.array(fl.read().splitlines())
    fl.close()
    while True:
        x, y = shuffle(x, y)  # Shuffle DATA to avoid over-fitting
        for i in list(range(slices)):
            f_name = x[i * batch_size:(i * batch_size) + batch_size]
            labels = y[i * batch_size:(i * batch_size) + batch_size]
            X = []
            Y = []
            if i % 10 == 0 and augment_data is True:
                randint = np.random.random_integers(low=0, high=(len(MULTI_SCALE) - 1))
                multi_scale = MULTI_SCALE[randint]
                # print("Multi-scale updated to ", multi_scale)

            for filename, label in list(zip(f_name, labels)):
                bbox, label = label

                if not os.path.isfile(filename):
                    print('Image Not Found')
                    continue
                img = cv2.cvtColor(cv2.imread(filename), cv2.COLOR_BGR2RGB)
                height, width, _ = img.shape
                img = cv2.resize(img, (IMG_INPUT, IMG_INPUT))

                # Multi-scale training
                if augment_data:
                    new_height = int(IMG_INPUT * multi_scale)
                    new_width  = int(IMG_INPUT * multi_scale)
                    img = cv2.resize(img, (new_width, new_height))

                processed_img = preprocess_img(img)

                # convert label to int
                # @TODO softmax
                index_label = np.where(CLASSES == label)[0][0]
                one_hot = HIER_TREE.encode_label(index=index_label)

                # convert to relative
                box = bbox.to_relative_size((float(width), float(height)))
                X.append(processed_img)
                Y.append(np.concatenate([np.array(box), [1.0], one_hot]))

                if augment_data is True:
                    aug_level = augment_level.loc[augment_level['label'] == label, 'scaling_factor'].values[0]

                    for l in list(range(aug_level)):
                        # Create new image & bounding box
                        aug_img, aug_box = random_transform(img, bbox.to_opencv_format())

                        # if box is out-of-bound. skip to next image
                        p1 = (np.asarray([width, height]) - aug_box[0][0])
                        p2 = (np.asarray([width, height]) - aug_box[0][1])
                        if np.any(p1 < 0) or np.any(p2 < 0):
                            continue

                        processed_img = preprocess_img(aug_img)
                        aug_box = convert_opencv_to_box(aug_box)
                        aug_box = aug_box.to_relative_size((float(width), float(height)))

                        X.append(processed_img)
                        Y.append(np.asarray(np.concatenate([np.array(aug_box), [1.0], one_hot])))

            # Shuffle X, Y again
            X, Y = shuffle(np.array(X), np.array(Y))
            for z in list(range(int(len(X) / batch_size))):
                if augment_data:
                    grid_w = new_width  / SHRINK_FACTOR
                    grid_h = new_height / SHRINK_FACTOR
                else:
                    grid_w = IMG_INPUT / SHRINK_FACTOR
                    grid_h = IMG_INPUT / SHRINK_FACTOR

                # Construct detection mask
                y_batch = np.zeros((batch_size, int(grid_h), int(grid_w), N_ANCHORS, 5 + N_CLASSES))

                labels = Y[z * batch_size:(z * batch_size) + batch_size]

                # print("Grid W {} || GRID_H {}".format(grid_w, grid_h))
                for b in range(batch_size):
                    # Find the grid cell where the centroid of ground truth locates
                    center_x = labels[b][0] * grid_w
                    center_y = labels[b][1] * grid_h
                    r = int(np.floor(center_x))
                    c = int(np.floor(center_y))
                    # Imagine a 3-D output feature map which there are only one cell contains the ground truth
                    if r < grid_w and c < grid_h:
                        y_batch[b, c, r, :, 0:4] = N_ANCHORS * [labels[b][..., :4]]
                        y_batch[b, c, r, :, 4]   = N_ANCHORS * [1.0]
                        y_batch[b, c, r, :, 5:]  = N_ANCHORS * [labels[b][..., 5:]]
#                         print(b, c, r)
                yield X[z * batch_size:(z * batch_size) + batch_size], \
                      y_batch.reshape([batch_size, int(grid_h), int(grid_w), N_ANCHORS*(N_CLASSES + 5)])


def calc_augment_level(y, scaling_factor=5):
    """
    Calculate scale factor for each class in data set
    :param y:              List of labels data
    :param scaling_factor: how much we would like to augment each class in data set
    :return: 
    """
    categories, frequencies = np.unique(y[:,1], return_counts=True)  # Calculate how many images in one traffic sign
    mean = frequencies.mean(axis=0)  # average images per traffic sign

    df = pd.DataFrame({'label': categories, 'frequency': frequencies})
    df['scaling_factor'] = df.apply(lambda row: int(scaling_factor*(mean / row['frequency'])), axis=1)
    return df


def convert_opencv_to_box(box):
    x1, y1, x2, y2 = np.array(box).ravel()
    xc, yc, w, h = convert_bbox(x1, y1, x2, y2)
    bbox = Box(xc, yc, w, h)
    return bbox
